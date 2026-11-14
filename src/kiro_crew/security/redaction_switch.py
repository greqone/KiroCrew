"""The owner's one setting for credential redaction.

The setting
-----------
``enabled`` in the keystone record (:func:`config.loader.credential_redaction_path`) is
the ONE owner setting: credential redaction on or off. Its default is
:data:`DEFAULT_REDACTION_ENABLED`, the only place that default is written. With it off:

* the owner's Files view and its diff skip the credential plan (:func:`owner_view`,
  entered by ``handlers/files.py`` after the requester is verified as the owner);
* every other path :data:`kiro_crew.redaction_seams.OUTPUT_SEAMS` lists (the chat turn,
  the saved transcript, channel egress, the conversation replayed into the agent's
  prompt, and the shape passes over log lines) opens :func:`redaction_seams.output_seam`
  and skips it too;
* in all of them the structured detectors (``security/redaction_structured/``) are
  skipped with the plan, and the exfiltration-URL pass leaves every URL as written.

Outside those scopes ``redact_credentials`` is unconditional: a refusal gate that
decides by comparing ``redact(x)`` with ``x`` (an upload, a file name, a memory write) never
opens a seam, so a path nobody listed keeps redacting. The design is the opposite of a
global bypass on purpose: a scope a caller must open cannot be inherited by a sink that
forgot to opt out.

What the setting does NOT do: it never touches a request-BLOCKING decision. ``exfil.py``
refuses a command or URL through ``_contains_fixed_credential`` and its siblings, which read
the pattern table directly and never call the redaction pass; those gates and the scan that
gates a command (``scan_exfiltration_urls``) keep firing with the setting off.
:data:`kiro_crew.redaction_seams.ALWAYS_REDACTED` is the list.

Why the record is a keystone file and not ``config.json``
---------------------------------------------------------
Switching the scrubber off is an authorization, not a preference, and the party
it constrains is the agent. ``config.json`` is writable by any auto-approved
agent shell, so a setting stored there could be flipped by a prompt-injected
agent that then prints the secrets it can read. The record therefore lives at
:func:`config.loader.credential_redaction_path`, on the read+write KEYSTONE
floor (``security._CREW_SECRET_LEAVES``) beside ``aws_service_consent.json``:
``is_sensitive_path`` refuses the tool path and the OS sandbox mounts the leaf
read-only (readable, never writable, from a sandboxed shell), so the only writer
is the owner-gated dashboard handler.

Fail direction
--------------
A missing record reads :data:`DEFAULT_REDACTION_ENABLED`. Every other read that cannot
positively establish a boolean ``enabled`` -- an unreadable file, malformed JSON, a
non-boolean value -- answers ``True``: a record that cannot be read is redaction ON.

The engine selector
-------------------
The same record carries ``engine``: ``structured`` (the default) runs the structured
credential detectors in front of main's regex passes, ``legacy`` runs main's passes
alone. It picks which engine redacts while redaction is ON and cannot contradict the
setting: with ``enabled`` false no engine runs. The dashboard shows no control for it.
Fields the record carried before (``seams_enabled``) are not read.

On a governed host
------------------
Where a governance policy governs any scope, turning redaction off is the
administrator's call, not the owner's: only a top-tier policy that names BOTH
:data:`DISABLE_SCOPE` and :data:`SEAMS_DISABLE_SCOPE` with ``enabled: true`` lets the
setting stay off (:func:`owner_may_disable`). Two rows because the setting reaches the
owner's screen AND the external model provider and the logs, and an administrator who
refused either keeps it refused. A lower tier folded beneath the top one cannot supply
either opt-in. Without them the PUT refuses OFF and :func:`read_state` reads a stored
(or default) OFF as ON. A host with no governed scope is unchanged.

Read cost
---------
There is no cache. The one seam that consults the record per request is a handler that
already does its file I/O off the event loop, so it reads the keystone with
``asyncio.to_thread(read_state)`` once per request and hands the verdict to
:func:`owner_view`. Every other seam reads the process value the gateway publishes
(:func:`publish_verdict`).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from kiro_crew import redaction_seams

logger = logging.getLogger(__name__)

#: Whether the calling context is rendering for the OWNER'S OWN VIEW with the
#: switch OFF -- the only state in which ``redact_credentials`` stands down. A
#: ContextVar, not a global: an owner-view render on one task must not leak the
#: bypass into a Slack post the same gateway is making on another. The verdict
#: is supplied by the seam, read ONCE per request off the event loop, and held
#: for the scope's lifetime, so every redaction call inside one render agrees.
_OWNER_VIEW_BYPASS: ContextVar[bool] = ContextVar(
    "kirocrew_redaction_owner_view_bypass", default=False
)


@contextmanager
def owner_view() -> Iterator[None]:
    """Mark the enclosed redaction calls as the OWNER'S OWN VIEW with redaction OFF.

    Enter this ONLY at a seam whose audience is the dashboard owner reading their
    own disk, after the requester has been verified as the owner AND the switch
    has been read OFF the event loop and found disabled (``handlers/files.py``
    ``_owner_view_bypasses_credential_pass``). Inside it ``redact_credentials``
    returns its input unchanged; outside it the switch is never consulted. There
    is deliberately no ``bypass`` argument: a caller that has not established
    both facts simply does not enter the scope. Re-entrant and task-local.
    """
    token = _OWNER_VIEW_BYPASS.set(True)
    try:
        yield
    finally:
        _OWNER_VIEW_BYPASS.reset(token)


def credential_pass_bypassed() -> bool:
    """Whether ``redact_credentials`` should stand down for THIS call.

    True only inside an :func:`owner_view` scope, or inside a
    ``redaction_seams.output_seam`` scope opened while redaction is off, and never
    inside :func:`always_scan`. Outside them it is always False and reads nothing.
    """
    if _ALWAYS_SCAN.get():
        return False
    return _OWNER_VIEW_BYPASS.get() or redaction_seams.in_bypassed_seam()


#: Whether the calling context is a SAFETY decision inside a seam. A ContextVar for
#: :func:`owner_view`'s reason: the scope is task-local and cannot leak to another task.
_ALWAYS_SCAN: ContextVar[bool] = ContextVar("kirocrew_redaction_always_scan", default=False)


@contextmanager
def always_scan() -> Iterator[None]:
    """Run the enclosed credential passes in full, inside a seam or an owner view alike.

    For a check that decides whether something is SAFE to hand out (whether a blocked
    address can be opened without sending a credential), where a pass that stood down
    would answer "clean" because the owner chose not to redact what they read.
    Re-entrant and task-local.
    """
    token = _ALWAYS_SCAN.set(True)
    try:
        yield
    finally:
        _ALWAYS_SCAN.reset(token)


#: Whether the calling context is redacting CODE rather than prose. A ContextVar for
#: :func:`owner_view`'s reason: a scope the caller must open cannot be inherited by a
#: sink that forgot to opt out.
_CODE_TEXT: ContextVar[bool] = ContextVar("kirocrew_redaction_code_text", default=False)


@contextmanager
def code_text() -> Iterator[None]:
    """Mark the enclosed redaction calls as running over CODE, so pass 3 spares two shapes.

    Pass 3 reads a run of base64-alphabet characters as a bare secret key by its entropy
    and shape, a guess about PROSE. In code it rewrites what the author wrote on purpose
    and the program then runs on the redaction marker with no error. Inside this scope it
    leaves a run alone only when the run is an ordered alphabet (``abc...xyz``) or a payload
    of 64 characters or more behind a ``data:<type>;base64,`` header; any other run is judged
    exactly as outside. Every other pass keys on a fixed format and runs unchanged, as does
    the exfiltration-URL pass. A scope rather than an argument because
    ``platform.context.redact_via_context`` takes none; open it only through
    ``platform.context.redact_code_via_context``. Re-entrant and task-local.
    """
    token = _CODE_TEXT.set(True)
    try:
        yield
    finally:
        _CODE_TEXT.reset(token)


def bare_secret_pass_enabled() -> bool:
    """Whether pass 3 judges every run for THIS call: false only inside :func:`code_text`,
    where it spares an ordered alphabet and a data URI payload."""
    return not _CODE_TEXT.get()


#: Serialises the read-modify-write in :func:`set_enabled`. An in-process lock,
#: because the store has
#: exactly one writer (the owner-gated dashboard handler in the gateway process),
#: and a sibling lock FILE would be an agent-reachable artifact that could block
#: the owner from turning redaction back ON.
#: A re-entrant lock so the handler can hold it around its WHOLE
#: audit-then-write transaction (see ``transaction()``) while ``set_enabled``
#: takes it again for the write itself.
_STORE_LOCK = threading.RLock()


def transaction() -> threading.RLock:
    """The lock a caller holds around an audit-and-write pair.

    Two owner PUTs that interleave -- OFF audited, ON audited, ON written, OFF
    written -- would leave the SEL's latest record contradicting the persisted
    switch. Holding this lock from the first audit to the last write makes each
    pair atomic against the other, so the audit trail and the switch always
    agree on the order of changes.
    """
    return _STORE_LOCK


#: Whether credential redaction is ON when the owner has not chosen. THE ONE place the
#: default is written: flipping it is this one line. OFF is the maintainer's order; a
#: governed host reads it as ON regardless (:func:`read_state`).
DEFAULT_REDACTION_ENABLED = False

#: The engine selector's two values (``engine`` in the same record). The selector picks
#: which engine redacts while redaction is on; it is not the setting above.
ENGINE_STRUCTURED = "structured"
ENGINE_LEGACY = "legacy"
ENGINES = (ENGINE_STRUCTURED, ENGINE_LEGACY)


@dataclass(frozen=True)
class RedactionState:
    """The recorded setting, the engine selector, and when each last changed.

    ``enabled`` is the one owner setting: False means credential redaction is off on
    every path :mod:`kiro_crew.redaction_seams` lists. ``engine`` picks which engine
    redacts while it is on.
    """

    enabled: bool
    changed_at: str
    engine: str = ENGINE_STRUCTURED
    engine_changed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "changed_at": self.changed_at,
            "engine": self.engine,
            "engine_changed_at": self.engine_changed_at,
        }


def _path():
    # Deferred: this module is imported by ``security.redaction`` at package
    # load, and ``config.loader`` must not be pulled onto that path.
    from kiro_crew.config.loader import credential_redaction_path

    return credential_redaction_path()


def _engine_of(raw: object) -> str:
    """The recorded engine; anything but the exact word ``legacy`` is structured."""
    if isinstance(raw, dict) and raw.get("engine") == ENGINE_LEGACY:
        return ENGINE_LEGACY
    return ENGINE_STRUCTURED


def _parse(raw: object) -> RedactionState:
    """Interpret a decoded store: an absent ``enabled`` is the default, a non-boolean is ON."""
    if not isinstance(raw, dict):
        return RedactionState(enabled=True, changed_at="")
    enabled = raw.get("enabled", DEFAULT_REDACTION_ENABLED)
    return RedactionState(
        enabled=enabled if isinstance(enabled, bool) else True,
        changed_at=str(raw.get("changed_at", "")),
        engine=_engine_of(raw),
        engine_changed_at=str(raw.get("engine_changed_at", "")),
    )


#: The governance capability that lets the owner keep redaction off on a governed host
#: for the owner's own screen (the Files view).
DISABLE_SCOPE = "capabilities.credential_redaction_off"
#: The governance capability that lets the owner keep redaction off on a governed host
#: for what reaches the external model provider and the logs. A row of its own, so an
#: administrator who refused those two sinks keeps them refused.
SEAMS_DISABLE_SCOPE = "capabilities.credential_redaction_seams_off"


def missing_opt_in() -> str | None:
    """The governance scope that keeps credential redaction on for this host, or None.

    None on an ``oss`` host whose policy governs no scope. Otherwise None only when the
    top policy tier itself enables BOTH :data:`DISABLE_SCOPE` and
    :data:`SEAMS_DISABLE_SCOPE`; an LTS build always governs, and its baseline keeps
    both rows off. A ceiling that cannot be read names the first scope. See
    :func:`_policy_allows` for the folded-ceiling rule.
    """
    return next((s for s in (DISABLE_SCOPE, SEAMS_DISABLE_SCOPE) if not _policy_allows(s)), None)


def owner_may_disable() -> bool:
    """Whether this host lets the owner turn credential redaction off."""
    return missing_opt_in() is None


def _policy_allows(scope: str) -> bool:
    """Whether the policy layer lets the owner turn off the switch *scope* governs.

    The tier ladder lets a lower tier add a scope its authority leaves unnamed, so
    in a folded ceiling (``lower_tier_folded``) the opt-in may come from the lower
    tier (a home or env document), not the administrator's. The folded ceiling does
    not say which tier named it, so a folded ceiling answers False. shortcut: this
    also refuses an opt-in the top tier named itself while a lower tier is present;
    read the authority tier's own row if a fleet needs that.
    """
    try:
        # circular import: platform.context loads the security package, this module included.
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import (
            _SANDBOX_FLAGS_SCOPE,
            CapabilityGate,
            policy_level_control,
        )

        ceiling = current_context().governance
        gate = policy_level_control(ceiling, scope)
    except Exception:
        logger.warning("governance is unreadable; credential redaction stays ON")
        return False
    if gate is None:
        # The reserved sandbox boot flags are inert, not a governed scope.
        return ceiling is None or not set(ceiling.controls) - {_SANDBOX_FLAGS_SCOPE}
    folded = ceiling is not None and ceiling.lower_tier_folded
    return isinstance(gate, CapabilityGate) and gate.enabled and not folded


def _read_stored() -> RedactionState:
    """The record on disk, before the governance rule. A missing record is the default."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RedactionState(enabled=DEFAULT_REDACTION_ENABLED, changed_at="")
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (non-UTF-8 bytes).
        logger.warning("credential-redaction record is unreadable; redaction stays ON")
        return RedactionState(enabled=True, changed_at="")
    return _parse(raw)


def _in_force(state: RedactionState) -> RedactionState:
    """*state* under the governance rule; every setter returns this, not the stored record.

    An OFF with no administrator opt-in reads as ON.
    """
    if not state.enabled and not owner_may_disable():
        state = dataclasses.replace(state, enabled=True)
    return state


def read_state() -> RedactionState:
    """The setting in force and the engine selector, read fresh from disk.

    A stored (or default) OFF reads as ON when :func:`owner_may_disable` says no; the
    engine selector is not governed.
    """
    return _in_force(_read_stored())


def read_engine() -> str:
    """The engine selector, read fresh from disk; a missing or unreadable record is structured.

    No governance read: either engine removes every secret main removes, so the
    choice needs no administrator's opt-in.
    """
    return _read_stored().engine


def _write(state: RedactionState) -> None:
    """Persist *state*; the caller holds :data:`_STORE_LOCK`.

    Rewritten in place, so a running Linux sandbox's read-only mount of it stays
    sealed; a torn read during the rewrite reads as ON. ``restrict_to_owner=True``
    is the fail-loud lockdown the sibling keystone stores use: the owner-only mode
    lands before any content, and a record it cannot protect is not written.
    """
    from kiro_crew.atomic_write import rewrite_in_place

    rewrite_in_place(
        _path(),
        json.dumps(state.to_dict(), indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def set_enabled(enabled: bool, *, changed_at: str) -> RedactionState:
    """Persist the owner's setting, keeping the engine as stored."""
    with _STORE_LOCK:
        state = dataclasses.replace(_read_stored(), enabled=bool(enabled), changed_at=changed_at)
        _write(state)
    return _in_force(state)


def set_engine(engine: str, *, changed_at: str) -> RedactionState:
    """Persist the engine selector, keeping the setting as stored."""
    if engine not in ENGINES:
        raise ValueError(f"unknown redaction engine: {engine!r}")
    with _STORE_LOCK:
        state = dataclasses.replace(_read_stored(), engine=engine, engine_changed_at=changed_at)
        _write(state)
    return _in_force(state)


def publish_verdict(state: RedactionState) -> None:
    """Tell this process's output seams the setting *state* holds (the gateway only)."""
    redaction_seams.publish(not state.enabled)
