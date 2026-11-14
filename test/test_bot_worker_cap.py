# SPDX-License-Identifier: Apache-2.0
"""A Bot's chat may keep at most ``max_workers`` of its own workers open (#37361).

The owner sets the limit on the Bot's settings page (``PUT /api/agents/<name>``);
``session_create`` refuses the next worker past it with ``bot_worker_cap_reached``
and creates nothing. A closed worker frees its seat.
"""

from __future__ import annotations

import json

import pytest
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.config.sections_session import BOT_MAX_WORKERS_DEFAULT
from kiro_crew.dashboard.session_control import SessionControlError


def _set_cap(tmp_path, cap) -> None:
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["max_workers"] = cap
    path.write_text(json.dumps(config), encoding="utf-8")
    _invalidate_config_cache()


def test_a_bot_with_no_limit_set_takes_the_default(crewmate):  # noqa: F811
    assert KiroCrewConfig.load().agents["pixel"].max_workers == BOT_MAX_WORKERS_DEFAULT


@pytest.mark.parametrize("stored, read", [(0, 1), (99, 32), ("x", BOT_MAX_WORKERS_DEFAULT)])
def test_a_hand_edited_limit_is_held_in_range(crewmate, tmp_path, stored, read):  # noqa: F811
    _set_cap(tmp_path, stored)
    assert KiroCrewConfig.load().agents["pixel"].max_workers == read


def test_the_worker_past_the_limit_is_refused_and_nothing_is_created(crewmate, tmp_path):  # noqa: F811
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    _create(state, caller)
    _create(state, caller)
    before = set(state._slots)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"
    assert err.value.status == 409
    assert "allows 2" in str(err.value)
    assert set(state._slots) == before


def test_closing_a_worker_frees_its_seat(crewmate, tmp_path):  # noqa: F811
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    first = _create(state, caller)["target"]
    with pytest.raises(SessionControlError):
        _create(state, caller)
    state._slots.pop(first)
    assert _create(state, caller)["target"] != first
