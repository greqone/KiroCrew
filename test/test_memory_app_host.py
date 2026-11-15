"""The memory app host facade: one audited door from ``memoryAdmin`` to listed owner routes.

``/api/memory/provider/host/<rest>`` dispatches to ``/api/<rest>`` only for a route in
``HOST_ROUTES``, only for the owner or an app holding ``permissions.memoryAdmin``, and the
grant it hands the inner route lives in request state, where no header can put it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import _shared_access
from kiro_crew.dashboard.handlers import memory_app_host as host
from kiro_crew.dashboard.handlers import memory_provider

pytestmark = pytest.mark.xdist_group("memory_provider")


async def _owner_route(request: web.Request) -> web.Response:
    refusal = await _shared_access.require_owner_dashboard_request(request, "memory.records")
    if refusal is not None:
        return refusal
    return web.json_response(
        {"grant": request.get(_shared_access.MEMORY_ADMIN_GRANT), "q": request.query.get("q")}
    )


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    app.router.add_route("*", "/api/memory/provider/host/{tail:.+}", host.api_memory_app_host)
    app.router.add_get("/api/memory/records", _owner_route)
    app.router.add_get("/api/config", _owner_route)  # an owner route the facade does not list
    return app


def _request(app: web.Application, path: str, *, app_name: str = "memory", headers=None):
    prefix = "/api/memory/provider/host/"
    tail = path.split("?", 1)[0][len(prefix) :] if path.startswith(prefix) else ""
    request = make_mocked_request(
        "GET", path, app=app, headers=headers or {}, match_info={"tail": tail}
    )
    request["user"] = "owner"
    request["app"] = app_name
    return request


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(memory_provider, "_audit", lambda *a, **k: seen.append(a))
    return seen


@pytest.mark.asyncio
async def test_a_memory_admin_app_reaches_a_listed_route_with_the_grant(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    response = await host.api_memory_app_host(
        _request(_app(), "/api/memory/provider/host/memory/records?store=default")
    )
    assert response.status == 200
    assert isinstance(response, web.Response) and response.text is not None
    assert json.loads(response.text) == {"grant": "app:memory", "q": None}
    assert audits[-1][:3] == ("app:memory", "host", "allowed")


@pytest.mark.asyncio
async def test_a_query_value_with_escaped_separators_reaches_the_route_as_sent(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    for raw, value in (("R%26D", "R&D"), ("c%2B%2B", "c++"), ("a%3Db", "a=b")):
        response = await host.api_memory_app_host(
            _request(_app(), f"/api/memory/provider/host/memory/records?store=default&q={raw}")
        )
        assert isinstance(response, web.Response) and response.text is not None
        assert json.loads(response.text)["q"] == value


@pytest.mark.asyncio
async def test_an_app_without_memory_admin_is_refused(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: False)
    with pytest.raises(web.HTTPForbidden) as refused:
        await host.api_memory_app_host(_request(_app(), "/api/memory/provider/host/memory/records"))
    assert json.loads(refused.value.text or "{}")["code"] == "memory_admin_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["config", "memory/../config", "nowhere"])
async def test_an_unlisted_route_is_not_found(monkeypatch, audits, tail):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    response = await host.api_memory_app_host(_request(_app(), f"/api/memory/provider/host/{tail}"))
    assert response.status == 404


@pytest.mark.asyncio
async def test_an_app_calling_the_owner_route_directly_is_still_refused(monkeypatch):
    # The grant is request state the facade sets; the route itself stays owner-only.
    request = _request(
        _app(), "/api/memory/records", headers={"X-Memory-Admin-Grant": "app:memory"}
    )
    response = await _owner_route(request)
    assert response.status in (401, 403)


def test_block_spans_tile_the_prompt_in_send_order():
    from kiro_crew.context_blocks import block_spans

    prompt = "intro\n[Memory tools]\nCall memory_recall.\n[CURRENT DATE] today\ntail"
    spans = block_spans(prompt)
    assert "".join(prompt[s:e] for _label, s, e in spans) == prompt
    assert [s for _label, s, _e in spans] == sorted(s for _label, s, _e in spans)
    assert "memory_tools" in [label for label, _s, _e in spans]
