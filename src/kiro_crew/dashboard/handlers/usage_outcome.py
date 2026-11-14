"""What a session delivered, set beside what it cost.

The Spend tab ranks conversations by credits. This module reads the same
conversations' own transcripts for the outcomes a person weighs that spend
against: commits, changed lines and pull requests, and how many of those pull
requests were merged. Nothing here runs git or calls a provider. Every figure
is read from text the gateway already holds. A tool row's command is the
``command`` argument the call was made with, or its title when it has none:

* a **commit** is counted when a shell tool row ran ``git commit`` and git's own
  summary line (``[branch abc1234] subject``) is in that row's output, with the
  ``N insertions(+), M deletions(-)`` line that follows it giving the changed
  lines. A quiet commit (``-q``) prints nothing when it succeeds, but a call that
  failed or was cancelled without output is stored with the same empty output,
  so silence proves nothing. A quiet commit is counted, with no line figures,
  only when ``&&`` joins it to the next command and the row's output is only
  ``git log --oneline`` lines: ``&&`` runs that log only after a commit was
  made. An amend, a failed commit and any other output are not counted;
* a **pull request** is counted when a shell tool row ran ``gh pr create`` or
  ``glab mr create`` and its output carries the new change's URL on a line of
  its own, parsed by the same validator the session chips use. An output that
  says the pull request already exists names one this call did not create, so
  it counts nothing;
* a pull request is **merged** when the gateway's cached chip status for that URL
  says so. That cache is read, never refreshed, so a pull request nobody has
  looked at yet is "unchecked", not "open".

Every rule above fails toward counting less. A figure here is never a guess that
something happened.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers.source_providers import (
    get_cached_check_status,
    is_owner_dashboard_request,
    parse_source_url,
)
from kiro_crew.dashboard.handlers.usage_store import TimeWindow, _parse_row_ts
from kiro_crew.tool_call_title import parse_tool_args

logger = logging.getLogger(__name__)

# How many of the costliest conversations are read. The rows arrive ranked by
# credits, so the cut drops the cheapest first. Reading is bounded work on every
# poll of the Spend tab, not a scan of every transcript on disk.
OUTCOME_TOP_CONVOS = 50

# A changed transcript is read again at most this often. The costliest sessions
# are the live ones, so their files change between polls of the Spend tab (every
# few seconds), and a long session's transcript runs to hundreds of megabytes:
# rereading it on every poll would hold a worker thread for seconds each time.
_REREAD_SECS = 60.0

# A tool row's output is read up to here, and no further: the summary lines this
# looks for open an output, and a pasted log should not set the cost of a poll.
_OUTPUT_READ_CHARS = 65536
_OUTPUT_READ_LINES = 400
# Room for one commit's summary and the statistics line that follows it.
_COMMIT_CHUNK_CHARS = 4000

_COMMAND_COMMIT = re.compile(r"\bgit\b[^\n|;&]{0,200}?\bcommit\b")
_COMMAND_AMEND = re.compile(r"--amend\b")
# ``-q``, ``-qm`` or ``--quiet`` given to the commit itself.
_COMMAND_QUIET = re.compile(
    r"\bgit\b[^\n|;&]{0,200}?\bcommit\b[^\n|;&]*?(?:\s-[A-Za-z]*q[A-Za-z]*(?=\s|$)|\s--quiet\b)"
)
# What joins a quiet commit to the command after it. Only ``&&`` runs that command
# when the commit succeeded alone; after ``;``, ``||``, a pipe or a newline a log
# line is printed whether or not the commit was made. Redirections such as
# ``2>&1`` are taken out first so their ``&`` is not read as one.
_REDIRECT = re.compile(r"\d*>&\d*-?|&>>?")
_NEXT_COMMAND = re.compile(r"&&|\|\||[;|&\n]")
# One ``git log --oneline`` line.
_ONELINE = re.compile(r"^([0-9a-f]{7,40}) ")
_COMMAND_PR = re.compile(r"\b(?:gh\s+pr|glab\s+mr)\s+create\b")
# A create for a branch that already has an open pull request prints that one's URL.
_PR_EXISTS = re.compile(r"\balready exists\b", re.IGNORECASE)
# ``[main abc1234] subject`` and ``[main (root-commit) abc1234] subject``.
_COMMIT_HEADER = re.compile(r"^\[[^\]\n]{1,200}? ([0-9a-f]{7,40})\]", re.MULTILINE)
_INSERTIONS = re.compile(r"(\d+) insertions?\(\+\)")
_DELETIONS = re.compile(r"(\d+) deletions?\(-\)")

# (kind, epoch, a, b, key): ``("commit", ts, added, removed, sha)`` or
# ``("pr", ts, 0, 0, identity)``.
Event = tuple[str, float, int, int, str]

_MEMO_MAX = 128
# Transcript key -> (file mtime, when it was read, its events).
_memo: OrderedDict[str, tuple[float, float, tuple[Event, ...]]] = OrderedDict()
_memo_lock = threading.Lock()


def _command_of(row: dict, meta: dict) -> str | None:
    """What a tool row ran: the ``command`` argument of the call, else its title.

    A long shell title can arrive cut short, which hides an ``--amend`` or what
    follows the commit; the call's own arguments hold the whole command.
    """
    args = parse_tool_args(meta.get("input"))
    command = args.get("command") if args else None
    if isinstance(command, str) and command.strip():
        return command
    title = row.get("content")
    return title if isinstance(title, str) else None


def _commit_events(ts: float, output: str) -> Iterable[Event]:
    headers = list(_COMMIT_HEADER.finditer(output))
    for i, header in enumerate(headers):
        stop = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        chunk = output[header.end() : min(stop, header.end() + _COMMIT_CHUNK_CHARS)]
        added = _INSERTIONS.search(chunk)
        removed = _DELETIONS.search(chunk)
        yield (
            "commit",
            ts,
            int(added.group(1)) if added else 0,
            int(removed.group(1)) if removed else 0,
            header.group(1),
        )


def _pr_events(ts: float, output: str) -> Iterable[Event]:
    if _PR_EXISTS.search(output):
        return
    for line in output.splitlines()[:_OUTPUT_READ_LINES]:
        text = line.strip()
        if not text.startswith("https://") or " " in text:
            continue
        try:
            ref = parse_source_url(text)
        except ValueError:
            continue
        if ref.kind == "change":
            yield ("pr", ts, 0, 0, ref.url)


def _quiet_commit_events(ts: float, command: str, output: str) -> Iterable[Event]:
    """The commit a returned ``git commit -q && git log --oneline`` names by its sha."""
    quiet = _COMMAND_QUIET.search(command)
    if quiet is None:
        return
    joined = _NEXT_COMMAND.search(_REDIRECT.sub(" ", command[quiet.end() :]))
    if joined is None or joined.group() != "&&":
        return
    lines = [line for line in output.splitlines() if line.strip()]
    first = _ONELINE.match(lines[0]) if lines else None
    if first and all(_ONELINE.match(line) for line in lines):
        yield ("commit", ts, 0, 0, first.group(1))


def outcome_events(rows: Iterable[Any]) -> tuple[Event, ...]:
    """The commits and pull requests one transcript's tool rows prove, in order.

    One linear pass. A row without a parseable time or a text output is skipped.
    """
    found: list[Event] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "tool":
            continue
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        command = _command_of(row, meta)
        output = meta.get("output")
        if command is None or not isinstance(output, str):
            continue
        is_commit = bool(_COMMAND_COMMIT.search(command)) and not _COMMAND_AMEND.search(command)
        is_pr = bool(_COMMAND_PR.search(command))
        if not (is_commit or is_pr):
            continue
        ts = _parse_row_ts(str(row.get("ts") or ""))
        if ts is None:
            continue
        text = output[:_OUTPUT_READ_CHARS]
        if is_commit:
            summaries = list(_commit_events(ts, text))
            found.extend(summaries)
            if not summaries and meta.get("done") is True:
                found.extend(_quiet_commit_events(ts, command, text))
        if is_pr:
            found.extend(_pr_events(ts, text))
    return tuple(found)


def _events_of(
    key: str,
    read_rows: Callable[[str], list[dict] | None],
    signature: Callable[[str], float | None],
    now: Callable[[], float],
) -> tuple[Event, ...]:
    """A transcript's events, reread only when its file changed, and then not
    more often than every ``_REREAD_SECS``."""
    stamp = signature(key)
    read_at = now()
    if stamp is not None:
        with _memo_lock:
            hit = _memo.get(key)
            if hit is not None and (hit[0] == stamp or read_at - hit[1] < _REREAD_SECS):
                _memo.move_to_end(key)
                return hit[2]
    try:
        rows = read_rows(key)
    except Exception:
        return ()
    events = outcome_events(rows or ())
    if stamp is not None:
        with _memo_lock:
            _memo[key] = (stamp, read_at, events)
            _memo.move_to_end(key)
            while len(_memo) > _MEMO_MAX:
                _memo.popitem(last=False)
    return events


def fold_outcome(
    events: Iterable[Event],
    *,
    start: float,
    end: float,
    credits: float,
    merged_of: Callable[[str], bool | None] | None,
) -> dict[str, Any] | None:
    """One session's outcome inside ``[start, end)``, or None when it delivered nothing.

    ``merged_of`` answers "was this pull request merged" from cached status, or
    None for a URL it has not seen; passing None for the callable itself omits
    the merge figures altogether.
    """
    commits: dict[str, tuple[int, int]] = {}
    prs: dict[str, None] = {}
    for kind, ts, added, removed, key in events:
        if ts < start or ts >= end:
            continue
        if kind == "commit":
            commits.setdefault(key, (added, removed))
        else:
            prs.setdefault(key)
    if not commits and not prs:
        return None
    out: dict[str, Any] = {
        "commits": len(commits),
        "lines_added": sum(a for a, _ in commits.values()),
        "lines_removed": sum(r for _, r in commits.values()),
        "prs": len(prs),
    }
    if merged_of is not None and prs:
        states = [merged_of(url) for url in prs]
        merged = sum(1 for s in states if s is True)
        out["merged"] = merged
        out["unchecked"] = sum(1 for s in states if s is None)
        if merged:
            out["per_merged"] = credits / merged
    return out


def with_outcomes(
    cost: dict[str, Any],
    *,
    read_rows: Callable[[str], list[dict] | None],
    signature: Callable[[str], float | None],
    transcript_key: Callable[[str], str],
    merged_of: Callable[[str], bool | None] | None,
    start: float,
    end: float = math.inf,
    places: int = 1,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """*cost* with an ``outcome`` on each of its costliest conversations that has one.

    The rows are copied, never written into: *cost* is the memoised object
    ``cost_breakdown`` hands every caller. ``outcome_scan`` names how many
    conversations were read, so a row without an ``outcome`` is told apart from
    one that was not read. A window in which nothing was delivered comes back as
    the same object, so its payload is what it was before this module.
    """
    conversations = cost.get("conversations")
    if not conversations:
        return cost
    transcripts = cost.get("transcripts") or {}
    rows: list[dict[str, Any]] = []
    delivered = False
    for i, row in enumerate(conversations):
        if i >= OUTCOME_TOP_CONVOS:
            rows.extend(conversations[i:])
            break
        slot = str(row.get("slot") or "")
        try:
            key = transcripts.get(slot) or transcript_key(slot)
        except Exception:
            key = ""
        outcome = None
        if key:
            outcome = fold_outcome(
                _events_of(key, read_rows, signature, now),
                start=start,
                end=end,
                credits=float(row.get("credits") or 0.0),
                merged_of=merged_of,
            )
        if outcome is None:
            rows.append(row)
            continue
        if "per_merged" in outcome:
            outcome["per_merged"] = round(outcome["per_merged"], places)
        delivered = True
        rows.append({**row, "outcome": outcome})
    if not delivered:
        return cost
    return {
        **cost,
        "conversations": rows,
        "outcome_scan": min(len(conversations), OUTCOME_TOP_CONVOS),
    }


def _cached_merge_state(url: str) -> bool | None:
    """Whether the chip cache holds *url* as merged; None when it holds no state."""
    state = (get_cached_check_status(url) or {}).get("state")
    return None if not state else state == "merged"


async def attach_session_outcomes(
    request: Any, cost: dict[str, Any], window: TimeWindow
) -> dict[str, Any]:
    """:func:`with_outcomes` for one ``/api/telemetry/startup`` request.

    Best effort, like every other block of that response: a transcript store that
    cannot be read leaves the spend view as it was. Merge state is the gateway's
    cached provider status, so only the owner is given it, as with the session
    chips.
    """
    try:
        log = getattr(request.app["state"], "conversation_log", None)
        if log is None:
            return cost
        try:
            owner = is_owner_dashboard_request(request)
        except Exception:
            owner = False
        return await asyncio.to_thread(
            with_outcomes,
            cost,
            read_rows=log.read_messages_chained_full,
            signature=log.session_mtime,
            transcript_key=slot_transcript_key,
            merged_of=_cached_merge_state if owner else None,
            start=window.start,
            end=math.inf if window.rolling else window.end,
            places=4 if cost.get("unit") == "usd" else 1,
        )
    except Exception:
        logger.debug("session outcome attachment failed", exc_info=True)
        return cost
