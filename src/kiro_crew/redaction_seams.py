"""The output seams the owner's one redaction setting governs.

``credential_redaction.json`` carries ONE owner setting, ``enabled``
(``security.redaction_switch``): credential redaction on or off. Its default is
:data:`~kiro_crew.security.redaction_switch.DEFAULT_REDACTION_ENABLED`.

This module is the ONE list of what ``enabled: false`` turns off besides the owner's
Files view (``redaction_switch.owner_view``). A seam is a call site, or a whole entry
point, that wraps its credential passes in :func:`output_seam`; inside that scope, with
the setting off, ``security.redaction``'s credential plan returns no spans, so both the
plan's own passes and the structured detectors are skipped, and the exfiltration-URL
pass leaves every URL as written. Every redaction call
that does not open the scope keeps redacting whatever the owner chose. That includes
every refusal gate that decides by comparing ``redact(x)`` with ``x``, so a seam left
off this list fails closed.

What stays on with the setting off is :data:`ALWAYS_REDACTED`.

Where the verdict comes from
----------------------------
A process-wide value, :func:`publish`-ed by the gateway when it boots, on every
governance refresh and on every owner PUT
(``dashboard.handlers.credential_redaction``). Nothing on a seam reads a file: a
log record or a prompt build never waits on disk. A process that never publishes
(an MCP server, a cron script, the CLI) keeps the default, ``False``, and so
keeps redacting. That is the fail direction: a stale or missing verdict redacts.

An import leaf (stdlib only): ``log_redaction`` imports it, and that module
installs at CLI bootstrap, before the ``security`` package can load.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

#: The earlier conversation replayed into the agent's prompt.
ACP_PROMPT = "acp_prompt"
#: The shape-based passes over operational log lines.
LOGS = "logs"
#: The dashboard chat turn: the live stream, the saved reply, tool and approval cards.
CHAT = "chat"
#: The saved transcript, written and read back.
TRANSCRIPT = "transcript"
#: What reaches a messaging channel: Slack, Discord, Telegram and the others.
CHANNELS = "channels"

#: Every output seam the setting covers, with the call sites that open it.
OUTPUT_SEAMS: Mapping[str, str] = MappingProxyType(
    {
        ACP_PROMPT: (
            "the conversation history replayed into the agent's prompt: "
            "context.build_session_replay and context_assembly.session_start"
        ),
        LOGS: (
            "the shape-based passes over log records: log_redaction.SecretRedactionFilter. "
            "Not platform.context.redact_log_via_context: its callers clip the result "
            "and send it to sinks that stay redacted"
        ),
        CHAT: "the chat turn: dashboard.chat_runner._run_chat and the side-question stream",
        TRANSCRIPT: "the transcript save and its load pass: slot_persistence.message_entries",
        CHANNELS: "channel egress: messaging.egress, the turn driver and the Slack turn",
    }
)

#: What stays redacted with the setting off.
ALWAYS_REDACTED: tuple[str, ...] = (
    "vault secret values in logs (log_redaction.SecretRedactionFilter's literal pass)",
    "request-blocking decisions about a URL or command (security.exfil scan_exfiltration_urls, "
    "_contains_fixed_credential, the OAuth consent-URL gate): decisions, not text rewriting",
    "request-blocking gates, and every refusal gate that compares redact(x) with x",
    "the dashboard's live log stream and its ring replay, which reach non-owner sockets "
    "(dashboard.handlers.updates_streams._stream_safe)",
    "a loaded companion's own extra patterns on log lines",
)

#: Whether the owner has turned redaction off, as last published in this process.
_redaction_off = False
_generation = 0

#: The seam the calling context is inside with the setting off, or "".
_IN_SEAM: ContextVar[str] = ContextVar("kirocrew_redaction_output_seam", default="")


def publish(off: bool) -> None:
    """Record the setting's position for this process (the gateway's writers only)."""
    global _redaction_off, _generation
    if bool(off) != _redaction_off:
        _generation += 1
    _redaction_off = bool(off)


def generation() -> int:
    """How many times the published position changed: a cache keyed on it never outlives a flip."""
    return _generation


def published_off() -> bool:
    """Whether the setting was last published as off in this process."""
    return _redaction_off


@contextmanager
def output_seam(name: str) -> Iterator[None]:
    """Mark the enclosed credential passes as the output seam *name*.

    With the setting off, ``security.redaction_switch.credential_pass_bypassed`` is
    True inside the scope; with it on, the scope changes nothing. An unknown name
    is a programming error and raises, so a seam cannot open under a misspelling
    that this table does not list. Re-entrant and task-local.
    """
    if name not in OUTPUT_SEAMS:
        raise ValueError(f"not an output seam: {name!r}")
    if not _redaction_off:
        yield
        return
    token = _IN_SEAM.set(name)
    try:
        yield
    finally:
        _IN_SEAM.reset(token)


def in_seam(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: run the coroutine function (or function) inside :func:`output_seam`.

    For an entry point whose body is too long to indent under a ``with``; generators
    are not supported.
    """
    if name not in OUTPUT_SEAMS:
        raise ValueError(f"not an output seam: {name!r}")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if fn.__code__.co_flags & _CO_COROUTINE:

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                if not _redaction_off:
                    return await fn(*args, **kwargs)
                with output_seam(name):
                    return await fn(*args, **kwargs)

            return run_async

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            if not _redaction_off:
                return fn(*args, **kwargs)
            with output_seam(name):
                return fn(*args, **kwargs)

        return run

    return decorate


#: ``code.co_flags`` bit of a coroutine function, so no ``inspect`` import on this leaf.
_CO_COROUTINE = 0x80


def in_bypassed_seam() -> bool:
    """Whether the calling context is inside an output seam while the setting is off NOW.

    A turn opened while redaction was off stops bypassing the moment it is published on.
    """
    return _redaction_off and bool(_IN_SEAM.get())
