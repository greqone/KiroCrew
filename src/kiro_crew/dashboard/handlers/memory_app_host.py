"""Host routes a memory manager app needs: the prompt stack, the review queues, and the
``permissions.memoryAdmin`` facade that lets such an app reach Crew's own memory routes.

| Route | Who may call |
|---|---|
| ``GET /api/memory/context-stack?store=`` | the owner (or the facade below) |
| ``GET /api/memory/review?store=&queue=`` | the owner (or the facade below) |
| ``POST /api/memory/review/{id}?store=`` | the owner (or the facade below) |
| ``* /api/memory/provider/host/<route>`` | the owner, or an app holding ``memoryAdmin`` |

**The facade.** ``permissions.memoryAdmin`` puts the one prefix ``/api/memory/provider``
on an app's API allowlist (``token_auth_app_scope``). ``/api/memory/provider/host/<rest>``
dispatches to ``/api/<rest>`` only when that method and route are on
:data:`HOST_ROUTES`, after :func:`memory_provider._caller` has checked the flag again and
audited the call. The inner request carries ``MEMORY_ADMIN_GRANT`` in its server-side
state, which :func:`require_owner_dashboard_request` admits; every other gate the route
applies (store resolution, the memory write gate, content screens) still runs. The
owner-only routes themselves stay closed to every app token, and a route not on the
list answers 404 through the facade.

**The prompt stack** is a real session-start prompt for the store, built by the same
``build_session_context`` a new chat runs, split by the block markers the turn trace
measures (``context_blocks.block_spans``). It is a preview: no turn, no session key.

**Review queues.** ``conflicts`` are pending proposals in ``memory_revisions`` against a
record's current revision; accepting writes the proposed value through the bulk-edit
preview and apply, rejecting keeps the current value and closes the proposal the same
way. ``dreams`` are Dreaming's approval list (``memory_dream``); accepting applies the
change it describes (retire the folded rows, the merged sentence, the tier move) through
the same edit path, and either decision removes it from the list. ``replaced`` are
episodes a semantic write retired; accepting restores one in place, rejecting hides it
from the queue (``memory_meta`` key :data:`DISMISSED_KEY`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web
from yarl import URL

from kiro_crew import memory_edit
from kiro_crew._sqlite_compat import sqlite3

from ._shared import (
    MEMORY_ADMIN_GRANT,
    _admin_store,
    _redact_memory_field,
    _store_unavailable,
    read_bounded_json,
    require_owner_dashboard_request,
    vector_memory_for_store,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory_app_host")

FACADE_PREFIX = "/api/memory/provider/host"

#: ``(method, route canonical)`` pairs the facade dispatches. Reads and the edits a
#: memory manager makes; nothing that changes config, members, apps or sessions.
HOST_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/memory/stores"),
        ("GET", "/api/members"),
        ("GET", "/api/memory/context-stack"),
        ("GET", "/api/memory/context-preview"),
        ("GET", "/api/memory/records"),
        ("POST", "/api/memory/records/refresh"),
        ("GET", "/api/memory/records/history"),
        ("POST", "/api/memory/bulk/preview"),
        ("POST", "/api/memory/bulk/apply"),
        ("GET", "/api/memory/review"),
        ("POST", "/api/memory/review/{id}"),
        ("GET", "/api/memory/preferences"),
        ("PUT", "/api/memory/preferences"),
        ("GET", "/api/memory/projects"),
        ("PUT", "/api/memory/projects"),
        ("GET", "/api/memory/retired"),
        ("POST", "/api/memory/retired/restore"),
        ("GET", "/api/members/{slug}/rules"),
        ("PUT", "/api/members/{slug}/rules"),
        ("GET", "/api/members/{slug}/goals"),
        ("PUT", "/api/members/{slug}/goals/long-term"),
        ("GET", "/api/members/{slug}/soul"),
        ("PUT", "/api/members/{slug}/soul"),
        ("GET", "/api/steering"),
        ("GET", "/api/steering/{key}"),
        ("PUT", "/api/steering/{key}"),
        ("GET", "/api/chat/slots/{slot}/steering"),
        ("PUT", "/api/chat/slots/{slot}/steering"),
    }
)

QUEUES = ("conflicts", "dreams", "replaced")
DISMISSED_KEY = "memory_app:dismissed_replaced"
_QUEUE_LIMIT = 200
_DISMISSED_CAP = 2000
_BLOCK_TEXT_CAP = 40_000


def _error(message: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


# ── facade ────────────────────────────────────────────────────────────────────


async def api_memory_app_host(request: web.Request) -> web.StreamResponse:
    """``/api/memory/provider/host/<rest>`` -> ``/api/<rest>`` for an allowed route."""
    from kiro_crew.dashboard.handlers import memory_provider

    caller = await memory_provider._caller(request, "host")
    tail = request.match_info.get("tail", "")
    inner_path = "/api/" + tail.lstrip("/")
    if ".." in PurePosixPath(inner_path).parts:
        return _error("not found", "not_found", 404)
    # The decoded query, rebuilt: re-encoding ``query_string`` would turn an escaped
    # ``&``, ``=`` or ``+`` in a value into a literal ``%26`` for the inner handler.
    url = URL.build(path=inner_path, query=request.rel_url.query)
    inner = request.clone(rel_url=url)
    match = await request.app.router.resolve(inner)
    route = getattr(match, "route", None)
    resource = getattr(route, "resource", None)
    canonical = getattr(resource, "canonical", "")
    if (
        getattr(match, "http_exception", None) is not None
        or (request.method, canonical) not in HOST_ROUTES
    ):
        return _error("not found", "not_found", 404)
    match.add_app(request.app)
    match.freeze()
    inner._match_info = match  # type: ignore[attr-defined]
    if caller.startswith("app:"):
        inner[MEMORY_ADMIN_GRANT] = caller
    return await match.handler(inner)


# ── shared reads ─────────────────────────────────────────────────────────────


def _record_text(kind: str, snapshot: dict | None) -> str:
    """What a person reads for a stored row (a revision snapshot or a live row)."""
    if not snapshot:
        return ""
    if kind == "episode":
        return str(snapshot.get("text") or "")
    raw = snapshot.get("value_json")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return str(raw or "")
    if isinstance(value, dict):
        rule = value.get("rule")
        if isinstance(rule, str):
            return rule
        text = value.get("text") or value.get("value")
        if isinstance(text, str):
            return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _lesson_tier(raw: Any) -> str | None:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    applies = value.get("applies")
    return applies if applies in ("always", "on_topic") else "unstated"


def _plain_id(record_id: str) -> tuple[str, str]:
    """``memory_record_meta.record_id`` -> the records API's ``(kind, id)``."""
    if record_id.startswith("key:"):
        key = record_id[4:]
        return ("directive" if key.startswith("lesson.") else "fact"), key
    return "episode", record_id


def _iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _dream_id(entry: dict) -> str:
    raw = json.dumps([entry.get("run"), entry.get("kind"), entry.get("keys")], sort_keys=True)
    return "dream:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


_DREAM_OPS = {
    "exact_copy": "merge",
    "near_duplicate": "merge",
    "uncertain_merge": "merge",
    "work_state": "expire",
    "legacy_tier": "demote",
    "numbered_family": "group",
}
_DREAM_REASONS = {
    "exact_copy": "Two lessons say exactly the same thing. The first stays; the copy retires.",
    "near_duplicate": "Lessons that say almost the same thing. The first stays; the others retire.",
    "uncertain_merge": "Two close lessons. The model wrote one sentence that keeps both.",
    "work_state": "Work state that git, GitHub or the work ledger already holds.",
    "legacy_tier": "A lesson the agent inferred, sent on every prompt. It would apply only on topic.",
    "numbered_family": "Keys that differ only by their numbers. Grouped for review; nothing changes.",
}


def _live_semantic(db: Any, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    rows = db.execute(
        f"SELECT key, value_json, source, updated_at FROM semantic_memory "  # nosec B608
        f"WHERE is_deleted = 0 AND key IN ({marks})",
        keys,
    ).fetchall()
    return {str(r["key"]): dict(r) for r in rows}


def _read_dismissed(db: Any) -> list[str]:
    row = db.execute("SELECT value FROM memory_meta WHERE key = ?", (DISMISSED_KEY,)).fetchone()
    try:
        value = json.loads(row["value"]) if row else []
    except (ValueError, TypeError):
        return []
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _queue_items(tier: Any, queue: str) -> list[dict]:
    """One review queue of *tier* as the Memory app's proposal rows. Read-only."""
    from kiro_crew import memory_dream

    items: list[dict] = []
    if queue == "conflicts":
        with tier._db_lock:
            rows = tier.db.execute(
                "SELECT r.id, r.record_id, r.operation, r.source, r.before_json, r.after_json, "
                "r.created_at, m.kind FROM memory_revisions r JOIN memory_record_meta m "
                "ON m.record_id = r.record_id WHERE r.status = 'conflict' "
                "AND r.base_revision = m.revision ORDER BY r.id DESC LIMIT ?",
                (_QUEUE_LIMIT,),
            ).fetchall()
        for row in rows:
            kind, plain = _plain_id(str(row["record_id"]))
            before = json.loads(row["before_json"]) if row["before_json"] else None
            after = json.loads(row["after_json"]) if row["after_json"] else None
            forget = row["operation"] == "forget"
            items.append(
                {
                    "id": f"conflict:{row['id']}",
                    "queue": "conflicts",
                    "op": "expire" if forget else "correct",
                    "record_id": plain,
                    "kind": kind,
                    "tier": (
                        _lesson_tier((after or before or {}).get("value_json"))
                        if kind == "directive"
                        else None
                    ),
                    "before": _record_text(kind, before),
                    "after": None if forget else _record_text(kind, after),
                    "reason": (
                        "A later chat suggested forgetting this row. It waits for you."
                        if forget
                        else "A later chat disagrees with this row. It waits for you."
                    ),
                    "source": str(row["source"] or ""),
                    "at": _iso_to_epoch(row["created_at"]),
                }
            )
    elif queue == "dreams":
        with tier._db_lock:
            proposals = memory_dream.read_proposals(tier.db)
            live = _live_semantic(
                tier.db, sorted({str(k) for p in proposals for k in p.get("keys", [])})
            )
        for entry in proposals[:_QUEUE_LIMIT]:
            keys = [str(k) for k in entry.get("keys", []) if isinstance(k, str)]
            changes = [str(k) for k in entry.get("changes", []) if isinstance(k, str)]
            keep = next((k for k in keys if k not in changes), keys[0] if keys else "")
            kind_name = str(entry.get("kind") or "")
            op = _DREAM_OPS.get(kind_name, "group")
            target = changes[0] if op in ("expire", "demote") and changes else keep
            row = live.get(target)
            text = _record_text("fact", row)
            proposed: str | None = text
            if kind_name == "uncertain_merge" and isinstance(entry.get("text"), str):
                proposed = entry["text"]
            if op == "expire":
                proposed = None
            items.append(
                {
                    "id": _dream_id(entry),
                    "queue": "dreams",
                    "op": op,
                    "record_id": target,
                    "kind": "directive" if target.startswith("lesson.") else "fact",
                    "tier": _lesson_tier(row.get("value_json")) if row else None,
                    "before": text,
                    "after": proposed,
                    "related": [k for k in keys if k != target],
                    "reason": _DREAM_REASONS.get(kind_name, ""),
                    "source": f"dreaming · {kind_name} · run {entry.get('run', '')}",
                    "at": None,
                }
            )
    elif queue == "replaced":
        with tier._db_lock:
            dismissed = set(_read_dismissed(tier.db))
        for row in tier.get_retired_episodic(limit=_QUEUE_LIMIT, offset=0):
            if str(row.get("id")) in dismissed:
                continue
            items.append(
                {
                    "id": f"replaced:{row.get('id')}",
                    "queue": "replaced",
                    "op": "retire",
                    "record_id": str(row.get("id")),
                    "kind": "episode",
                    "tier": None,
                    "before": str(row.get("text") or ""),
                    "after": row.get("superseded_by"),
                    "reason": "A newer fact retired this experience.",
                    "source": "semantic_update",
                    "at": _iso_to_epoch(row.get("retired_at")),
                }
            )
    return items


def _queue_counts(tier: Any) -> dict[str, int]:
    counts = {queue: len(_queue_items(tier, queue)) for queue in QUEUES}
    with tier._db_lock:
        # Only an experience records a recall; this matches the records filter.
        counts["never_recalled"] = int(
            tier.db.execute(
                "SELECT COUNT(*) FROM episodic_memories "
                "WHERE is_deleted = 0 AND last_accessed_at IS NULL"
            ).fetchone()[0]
        )
    return counts


# ── edits behind a review decision ──────────────────────────────────────────


def _edit(
    tier: Any, store: str, secret: bytes, identities: list[tuple[str, str]], operation: dict
) -> int:
    """Run one bulk edit over live *identities* through preview then apply; changed count."""
    entries = memory_edit.refresh_records(
        tier, [{"kind": kind, "id": ident} for kind, ident in identities]
    )["entries"]
    if not entries:
        return 0
    preview = memory_edit.preview_edit(
        tier,
        store,
        secret,
        {
            "selection": {
                "items": [
                    {"kind": e["kind"], "id": e["id"], "revision": e["revision"]} for e in entries
                ]
            },
            "operation": operation,
        },
    )
    result = memory_edit.apply_edit(tier, store, secret, preview["preview_id"])
    return int(result.get("changed_count", 0))


def _remove_dream(tier: Any, dream_id: str) -> dict | None:
    """Drop one entry from Dreaming's list; the entry, or ``None`` when it is gone."""
    from kiro_crew import memory_dream
    from kiro_crew import memory_kind_routing as kind_log

    now = datetime.now(timezone.utc).isoformat()
    with tier._db_lock:
        db = tier.db
        db.execute("BEGIN IMMEDIATE")
        try:
            listed = memory_dream.read_proposals(db)
            found = next((p for p in listed if _dream_id(p) == dream_id), None)
            if found is not None:
                kept = [p for p in listed if p is not found]
                kind_log._upsert(db, memory_dream.PROPOSALS_KEY, json.dumps(kept), now)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return found


def _dismiss(tier: Any, episode_id: str) -> None:
    from kiro_crew import memory_kind_routing as kind_log

    now = datetime.now(timezone.utc).isoformat()
    with tier._db_lock:
        db = tier.db
        db.execute("BEGIN IMMEDIATE")
        try:
            kept = [i for i in _read_dismissed(db) if i != episode_id] + [episode_id]
            kind_log._upsert(db, DISMISSED_KEY, json.dumps(kept[-_DISMISSED_CAP:]), now)
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _resolve(tier: Any, store: str, secret: bytes, item_id: str, decision: str) -> str:
    """Carry out one review decision. Returns the outcome word the app shows."""
    accept = decision == "accept"
    if item_id.startswith("conflict:"):
        revision_id = int(item_id.split(":", 1)[1])
        with tier._db_lock:
            row = tier.db.execute(
                "SELECT r.record_id, r.operation, r.after_json FROM memory_revisions r "
                "JOIN memory_record_meta m ON m.record_id = r.record_id WHERE r.id = ? "
                "AND r.status = 'conflict' AND r.base_revision = m.revision",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise memory_edit.MemoryEditError("This proposal is gone.", "review_item_gone", 404)
        kind, plain = _plain_id(str(row["record_id"]))
        refreshed = memory_edit.refresh_records(tier, [{"kind": kind, "id": plain}])
        if not refreshed["entries"]:
            raise memory_edit.MemoryEditError("This memory is gone.", "review_item_gone", 404)
        current = refreshed["entries"][0]
        if accept and row["operation"] == "forget":
            _edit(tier, store, secret, [(kind, plain)], {"type": "forget"})
            return "accepted"
        if accept:
            proposed = json.loads(row["after_json"] or "{}")
            if kind == "episode":
                operation: dict = {"type": "set", "text": proposed.get("text")}
            else:
                operation = {"type": "set", "value": json.loads(proposed.get("value_json"))}
        elif kind == "episode":
            operation = {"type": "set", "text": current["text"]}
        else:
            operation = {"type": "set", "value": json.loads(current["value_json"])}
        _edit(tier, store, secret, [(kind, plain)], operation)
        return "accepted" if accept else "rejected"
    if item_id.startswith("dream:"):
        entry = _remove_dream(tier, item_id)
        if entry is None:
            raise memory_edit.MemoryEditError("This proposal is gone.", "review_item_gone", 404)
        if not accept:
            return "rejected"
        kind_name = entry.get("kind")
        keys = [str(k) for k in entry.get("keys", []) if isinstance(k, str)]
        changes = [str(k) for k in entry.get("changes", []) if isinstance(k, str)]

        def ident(key: str) -> tuple[str, str]:
            return ("directive" if key.startswith("lesson.") else "fact", key)

        if kind_name == "legacy_tier":
            _edit(
                tier,
                store,
                secret,
                [ident(k) for k in changes],
                {"type": "set_tier", "applies": "on_topic"},
            )
        elif kind_name in ("exact_copy", "near_duplicate", "uncertain_merge", "work_state"):
            keep = next((k for k in keys if k not in changes), None)
            text = entry.get("text")
            if kind_name == "uncertain_merge" and keep and isinstance(text, str) and text.strip():
                refreshed = memory_edit.refresh_records(
                    tier, [{"kind": ident(keep)[0], "id": keep}]
                )
                if refreshed["entries"]:
                    value = json.loads(refreshed["entries"][0]["value_json"])
                    if isinstance(value, dict):
                        _edit(
                            tier,
                            store,
                            secret,
                            [ident(keep)],
                            {"type": "set", "value": dict(value, rule=text.strip())},
                        )
            if changes:
                _edit(tier, store, secret, [ident(k) for k in changes], {"type": "forget"})
        return "accepted"
    if item_id.startswith("replaced:"):
        episode_id = item_id.split(":", 1)[1]
        if accept:
            if not tier.restore_episodic(episode_id):
                raise memory_edit.MemoryEditError(
                    "This experience is gone.", "review_item_gone", 404
                )
            return "restored"
        _dismiss(tier, episode_id)
        return "rejected"
    raise memory_edit.MemoryEditError("Unknown review item.", "review_item_unknown", 404)


async def api_memory_review(request: web.Request) -> web.Response:
    """GET /api/memory/review?store=&queue=conflicts|dreams|replaced"""
    refusal = await require_owner_dashboard_request(request, "memory.review.list")
    if refusal is not None:
        return refusal
    name, refusal = await _admin_store(request, "memory.review.list")
    if refusal is not None:
        return refusal
    queue = request.query.get("queue", "conflicts")
    if queue not in QUEUES:
        return _error("queue must be conflicts, dreams or replaced.", "invalid_review_queue")
    tier = await vector_memory_for_store(request.app["state"], name)
    if tier is None:
        return _store_unavailable(name)
    try:
        items = await asyncio.to_thread(_queue_items, tier, queue)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    return web.json_response(_redact_memory_field({"store": name or "default", "items": items}))


async def api_memory_review_resolve(request: web.Request) -> web.Response:
    """POST /api/memory/review/{id}?store= -- ``{decision: accept|reject}``."""
    from kiro_crew.dashboard.handlers.memory import _memory_write_gate
    from kiro_crew.dashboard.handlers.memory_edit import _secret

    refusal = await require_owner_dashboard_request(request, "memory.review.resolve")
    if refusal is not None:
        return refusal
    body, refusal = await read_bounded_json(request, max_bytes=4096)
    if refusal is not None:
        return refusal
    assert body is not None
    name, refusal = await _admin_store(request, "memory.review.resolve")
    if refusal is not None:
        return refusal
    refusal = await _memory_write_gate(
        request.app["state"], request, "memory.review.resolve", store=name
    )
    if refusal is not None:
        return refusal
    decision = body.get("decision")
    if decision not in ("accept", "reject"):
        return _error("decision must be accept or reject.", "invalid_review_decision")
    item_id = request.match_info.get("id", "")
    state = request.app["state"]
    tier = await vector_memory_for_store(state, name)
    if tier is None:
        return _store_unavailable(name)
    try:
        outcome = await asyncio.to_thread(_resolve, tier, name, _secret(state), item_id, decision)
    except memory_edit.MemoryEditError as exc:
        return _error(str(exc), exc.code, exc.status)
    except ValueError:
        return _error("Unknown review item.", "review_item_unknown", 404)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    from ._shared import _audit

    await _audit(
        request, "memory.review.resolve", "success", f"{name or 'default'}:{item_id}:{decision}"
    )
    return web.json_response({"outcome": outcome})


# ── the member's SOUL.md ─────────────────────────────────────────────────────

SOUL_FILE = "SOUL.md"
SOUL_MAX_CHARS = 16_000


def _member_workspace_dir(slug: str) -> tuple[str, Any] | None:
    """``(member name, the member's own workspace directory)`` for *slug*, from config."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_locations import workspace_dir_from_entry

    config = KiroCrewConfig.load()
    for name, agent in config.agents.items():
        if str(getattr(agent, "member_id", "") or "") == slug:
            workspace = str(getattr(agent, "workspace", "") or "")
            entry = config.workspaces.get(workspace)
            if entry is None:
                return None
            return name, workspace_dir_from_entry(entry)
    return None


async def api_member_soul(request: web.Request) -> web.Response:
    """GET/PUT /api/members/{slug}/soul -- ``SOUL.md`` in the member's own workspace.

    The directory comes from the member's config record, never from the request, so a
    caller cannot name a path. A desk chat runs in that workspace, which is where the
    essentials builder reads the project's ``SOUL.md``.
    """
    from kiro_crew.atomic_write import atomic_write

    operation = "members.soul.write" if request.method == "PUT" else "members.soul.read"
    refusal = await require_owner_dashboard_request(request, operation)
    if refusal is not None:
        return refusal
    slug = request.match_info.get("slug", "")
    found = await asyncio.to_thread(_member_workspace_dir, slug)
    if found is None:
        return _error("No Bot with that id has a workspace.", "member_unknown", 404)
    member, directory = found
    path = directory / SOUL_FILE
    if request.method == "PUT":
        body, refusal = await read_bounded_json(request, max_bytes=SOUL_MAX_CHARS * 4 + 1024)
        if refusal is not None:
            return refusal
        assert body is not None
        text = body.get("text")
        if not isinstance(text, str) or len(text) > SOUL_MAX_CHARS:
            return _error(
                f"SOUL.md is text of at most {SOUL_MAX_CHARS} characters.", "invalid_soul"
            )
        if _redact_memory_field(text) != text:
            return _error("This looks like a secret. Keep secrets out of SOUL.md.", "soul_secret")
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(atomic_write, path, text)
        from ._shared import _audit

        await _audit(request, operation, "success", f"{slug}:{len(text)}")
        return web.json_response({"slug": slug, "member": member, "chars": len(text)})
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError):
        return _error("SOUL.md could not be read.", "soul_unreadable", 503)
    return web.json_response(
        {"slug": slug, "member": member, "text": text, "max_chars": SOUL_MAX_CHARS}
    )


# ── the prompt stack ────────────────────────────────────────────────────────


#: Section openers inside the session-start prompt, finer than the trace labels. Each is
#: matched at a line start, as a prefix (the member headers carry a dash and a gloss).
_MEMBER_MARKERS: tuple[tuple[str, str], ...] = (
    ("essentials_header", "[V2 ESSENTIAL CONTEXT"),
    ("member_identity", "[MEMBER IDENTITY]"),
    ("how_you_work", "[HOW YOU WORK]"),
    ("crew_log_tools", "[YOUR CREW LOG, DASHBOARD AND TOOLS]"),
    ("permanent_rules", "[PERMANENT RULES"),
    ("goals", "[YOUR GOALS"),
    ("briefing", "[CURRENT ASSIGNMENT"),
    ("dashboard", "[DASHBOARD]"),
    ("essential_source", "[Essential source: "),
    ("findings", "[Learned experience"),
)

#: An ``[Essential source: <path>]`` block, named by the file it carries.
_SOURCE_LABELS: dict[str, str] = {
    "SOUL.md": "soul_md",
    "preferences.md": "preferences_md",
    "projects.md": "projects_md",
    "AGENTS.md": "agents_md",
}

#: Who owns a block (which says who may edit it) and where it is edited.
_OWNERS: dict[str, tuple[str, str | None]] = {
    "critical_rules": ("product", None),
    "session_wrapper": ("product", None),
    "date": ("product", None),
    "agent_identity": ("product", None),
    "surface": ("product", None),
    "workspace_identity": ("product", None),
    "docs_pointer": ("product", None),
    "memory": ("learned", None),
    "memory_tools": ("product", None),
    "steering": ("files", "place"),
    "semantic_memory": ("learned", None),
    "task_facts": ("learned", None),
    "skill_index": ("product", None),
    "lessons": ("learned", "rules"),
    "episodic_memory": ("learned", None),
    "user_profile": ("settings", "settings"),
    "response_preferences": ("settings", "settings"),
    "ui_language": ("settings", "settings"),
    "tool_names": ("product", None),
    "member_identity": ("product", None),
    "how_you_work": ("product", None),
    "permanent_rules": ("user", "place"),
    "goals": ("user", "place"),
    "briefing": ("member", None),
    "essentials_header": ("product", None),
    "crew_log_tools": ("product", None),
    "dashboard": ("product", None),
    "soul_md": ("user", "place"),
    "preferences_md": ("user", "place"),
    "projects_md": ("user", "place"),
    "agents_md": ("files", None),
    "context_settings": ("settings", "settings"),
    "essential_source": ("files", None),
    "findings": ("learned", "rules"),
}


def _split_member(label: str, start: int, end: int, prompt: str) -> list[tuple[str, int, int]]:
    """Split one trace span further at the section openers it holds (line starts only)."""
    cuts: list[tuple[int, str]] = []
    for sub, marker in _MEMBER_MARKERS:
        at = prompt.find(marker, start, end)
        while at != -1:
            if at == 0 or prompt[at - 1] == "\n":
                name = sub
                if sub == "essential_source":
                    line_end = prompt.find("\n", at, end)
                    line = prompt[at : line_end if line_end != -1 else end]
                    source = line[len(marker) :].rstrip("]").strip()
                    base = source.rsplit("/", 1)[-1]
                    name = _SOURCE_LABELS.get(base) or (
                        "context_settings" if base.endswith("#context-settings") else sub
                    )
                cuts.append((at, name))
            at = prompt.find(marker, at + 1, end)
    if not cuts:
        return [(label, start, end)]
    cuts.sort()
    out: list[tuple[str, int, int]] = []
    if cuts[0][0] > start:
        out.append((label, start, cuts[0][0]))
    for i, (at, sub) in enumerate(cuts):
        out.append((sub, at, cuts[i + 1][0] if i + 1 < len(cuts) else end))
    return out


def _marker_line(text: str) -> str:
    first = text.strip().split("\n", 1)[0].strip()
    return first[:120]


def _lesson_counts(tier: Any) -> dict[str, int]:
    with tier._db_lock:
        rows = tier.db.execute(
            "SELECT value_json FROM semantic_memory WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
        ).fetchall()
        facts = tier.db.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE is_deleted = 0 AND key NOT LIKE 'lesson.%'"
        ).fetchone()[0]
        episodes = tier.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
        ).fetchone()[0]
    rules = sum(1 for r in rows if _lesson_tier(r["value_json"]) != "on_topic")
    return {
        "rules": rules,
        "findings": len(rows) - rules,
        "facts": int(facts),
        "experiences": int(episodes),
    }


def _recent(tier: Any, limit: int = 6) -> list[dict]:
    """The newest live rows of *tier*, for the app's rail. Read-only."""
    with tier._db_lock:
        sem = tier.db.execute(
            "SELECT key, value_json, updated_at FROM semantic_memory WHERE is_deleted = 0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        epi = tier.db.execute(
            "SELECT id, text, created_at FROM episodic_memories WHERE is_deleted = 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows = [
        {
            "kind": "directive" if str(r["key"]).startswith("lesson.") else "fact",
            "id": str(r["key"]),
            "text": _record_text("fact", dict(r)),
            "tier": _lesson_tier(r["value_json"]) if str(r["key"]).startswith("lesson.") else None,
            "updated_at": _iso_to_epoch(r["updated_at"]),
        }
        for r in sem
    ] + [
        {
            "kind": "episode",
            "id": str(r["id"]),
            "text": str(r["text"]),
            "tier": None,
            "updated_at": _iso_to_epoch(r["created_at"]),
        }
        for r in epi
    ]
    rows.sort(key=lambda r: r["updated_at"] or 0, reverse=True)
    return rows[:limit]


def _latest_slot(state: Any, store: str) -> str:
    """The key of the open chat that runs on *store*: a member thread names its store."""
    slots = dict(getattr(state, "_slots", {}) or {})
    mine = [
        key
        for key, slot in slots.items()
        if (getattr(slot, "memory_store", "") or "") == (store or "")
        or (store and str(key).endswith(f".memory-{store}"))
    ]
    return max(mine, key=lambda k: str(getattr(slots[k], "created_at", "") or "")) if mine else ""


def _last_turn(slot_key: str) -> dict[str, int] | None:
    """The newest session-start turn's blocks for *slot_key*, by trace label."""
    from kiro_crew.dashboard.handlers.usage import context_trace

    trace = context_trace(slot_key, 14)
    starts = [t for t in trace.get("turns", []) if t.get("first_start")]
    if not starts:
        return None
    return {str(k): int(v) for k, v in starts[-1].get("blocks", {}).items()}


def _build_stack(state: Any, store: str, chat: str) -> dict:
    """The context stack for *store*: blocks in send order, with sizes and owners."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context_blocks import block_spans

    config = KiroCrewConfig.load()
    record = config.memory_stores.get(store) if store else None
    member_name = str(getattr(record, "owner_member", "") or "")
    member_cfg = config.agents.get(member_name) if member_name else None
    version = int(getattr(record, "memory_version", 1) or 1) if record else 1
    builder = getattr(state, "context_builder", None)
    if builder is None:
        raise RuntimeError("the context builder is not ready")
    slug = str(getattr(member_cfg, "member_id", "") or "") if member_cfg else ""
    own = _member_workspace_dir(slug) if slug else None
    prompt = builder.build_session_context(
        None,
        agent=(getattr(member_cfg, "kiro_agent", "") or None) if member_cfg else None,
        memory_store=store or None,
        member=member_name,
        workspace=(getattr(member_cfg, "workspace", "") or None) if member_cfg else None,
        project=str(own[1]) if own else None,
    )
    spans: list[tuple[str, str, int, int]] = []
    for label, start, end in block_spans(prompt):
        spans.extend((sub, label, s, e) for sub, s, e in _split_member(label, start, end, prompt))
    slots = getattr(state, "_slots", {}) or {}
    slot_name = chat if chat and chat in slots else _latest_slot(state, store)
    traced = _last_turn(slot_name) if slot_name else None
    from kiro_crew.context_assembly.budget import _LESSONS_STARTUP_CAP
    from kiro_crew.member_essential_context import ESSENTIAL_MAX_CHARS

    open_at = prompt.find("[V2 ESSENTIAL CONTEXT")
    close_at = prompt.find("[END V2 ESSENTIAL CONTEXT]")
    envelope = (
        (open_at, close_at + len("[END V2 ESSENTIAL CONTEXT]")) if 0 <= open_at < close_at else None
    )
    caps = {"lessons": _LESSONS_STARTUP_CAP}
    blocks: list[dict] = []
    for index, (label, parent, start, end) in enumerate(spans):
        text = prompt[start:end]
        if not text.strip():
            continue
        owner, edit_in = _OWNERS.get(label, ("product", None))
        last = None
        in_envelope = bool(envelope and envelope[0] <= start < envelope[1])
        if traced is not None and (parent != "unclassified" or in_envelope):
            # The trace measures the coarse block; a section inside it went with it.
            # Outside the essentials envelope an unclassified span says nothing
            # about one section, so that block reads as untraced.
            sent = traced.get(parent)
            last = {"sent": bool(sent), "trace_label": parent, "trace_chars": sent or 0}
        blocks.append(
            {
                "id": (
                    f"{label}" if not any(b["id"] == label for b in blocks) else f"{label}#{index}"
                ),
                "label": label,
                "marker": _marker_line(text),
                "owner": owner,
                "edit_in": edit_in,
                "editable": edit_in == "place",
                "chars": len(text),
                "cap": caps.get(label),
                "group": "essential" if envelope and envelope[0] <= start < envelope[1] else None,
                "text": text[:_BLOCK_TEXT_CAP],
                "truncated": len(text) > _BLOCK_TEXT_CAP,
                "last_turn": last,
            }
        )
    groups = []
    if envelope:
        groups.append(
            {
                "id": "essential",
                "cap": ESSENTIAL_MAX_CHARS,
                "chars": sum(b["chars"] for b in blocks if b["group"] == "essential"),
            }
        )
    harness = _harness(config, member_cfg)
    if not any(b["label"] == "steering" for b in blocks):
        files = _steering_files(Path(own[1]) if own else None)
        if files:
            # The harness loads these as agent resources itself, so they are not
            # in Crew's own prompt text; they still ride the turn, ahead of it.
            # A member's essentials can quote a file too: that copy is measured
            # where it sits, so it is not counted twice here.
            native = [
                f for f in files if f["inclusion"] == "always" and f["text"].strip() not in prompt
            ]
            blocks.insert(
                0,
                {
                    "id": "steering",
                    "label": "steering",
                    "marker": "~/.kiro/steering/*.md",
                    "owner": "files",
                    "edit_in": "place",
                    "editable": True,
                    "chars": sum(f["chars"] for f in native),
                    "cap": None,
                    "group": None,
                    "text": None,
                    "truncated": False,
                    "files": files,
                    "native": True,
                    "last_turn": None,
                },
            )
    return {
        "store": store or "default",
        "member": {
            "slug": str(getattr(member_cfg, "member_id", "") or "") if member_cfg else "",
            "name": member_name or "Global",
            "version": version,
        },
        "harness": harness,
        "total_chars": len(prompt),
        "blocks": blocks,
        "groups": groups,
        # The trace keeps no clock time for a turn, so ``at`` stays empty.
        "last_turn": {"chat": slot_name, "at": None} if traced is not None else None,
    }


def _harness(config: Any, member_cfg: Any) -> str:
    """The ACP backend id a turn on this stack runs on (``kas``, ``kiro``, ``claude``...)."""
    from kiro_crew.agent_sdk.backends import resolve_selected_backend

    if member_cfg is not None:
        own = str(getattr(member_cfg, "acp_backend", "") or "")
        return resolve_selected_backend(
            own or str(getattr(config.agent, "member_acp_backend", "") or "kas")
        )
    return resolve_selected_backend(getattr(config.agent, "acp_backend", ""))


#: A steering file's text in the stack is cut here; the editor reads the whole file.
_STEERING_TEXT_CAP = 20_000


def _steering_files(project_dir: Path | None) -> list[dict]:
    """The steering files a turn on *project_dir* receives, with their text."""
    from kiro_crew.dashboard.handlers.steering import (
        _resolve_and_read_blocking,
        list_steering_blocking,
    )

    out: list[dict] = []
    for entry in list_steering_blocking(project_dir).get("files", []):
        if entry.get("inclusion") not in ("always", "fileMatch", "manual"):
            continue
        text = ""
        if entry.get("readable", True):
            text, _display, error = _resolve_and_read_blocking(str(entry["key"]), project_dir)
            if error:
                text = ""
        out.append(
            {
                "key": str(entry["key"]),
                "name": str(entry.get("name", "")),
                "path": str(entry.get("path", "")),
                "inclusion": str(entry["inclusion"]),
                "chars": len(text) if text else int(entry.get("size", 0) or 0),
                "enabled_in_chat": True,
                "text": text[:_STEERING_TEXT_CAP],
                "editable": bool(entry.get("editable", False)),
            }
        )
    return out


async def api_memory_context_stack(request: web.Request) -> web.Response:
    """GET /api/memory/context-stack?store=&chat= -- the session-start prompt, as blocks."""
    refusal = await require_owner_dashboard_request(request, "memory.context_stack")
    if refusal is not None:
        return refusal
    name, refusal = await _admin_store(request, "memory.context_stack")
    if refusal is not None:
        return refusal
    state = request.app["state"]
    tier = await vector_memory_for_store(state, name)
    if tier is None:
        return _store_unavailable(name)
    chat = request.query.get("chat", "").strip()[:200]
    try:
        stack = await asyncio.to_thread(_build_stack, state, name, chat)
        stack["recall"] = await asyncio.to_thread(_lesson_counts, tier)
        stack["queues"] = await asyncio.to_thread(_queue_counts, tier)
        stack["recent"] = await asyncio.to_thread(_recent, tier)
    except (OSError, sqlite3.Error, RuntimeError):
        logger.warning("context stack for %r failed", name or "default", exc_info=True)
        return _store_unavailable(name)
    return web.json_response(_redact_memory_field(stack))
