"""Kiro Space stores as App Store sources: the routes, the factory and the rows.

A store provider is what a build sets at ``app["market_registries"]``; the
open-source build sets none. These tests drive the routes with an in-memory
provider whose client answers canned ``ListingView`` dicts, and the real
``HttpMarketClient`` over an ``httpx.MockTransport`` for the CBOR round trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import _make_app_source, _setup_env

import kiro_crew.apps.market_client as market_client_mod
import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps import market_registries as mr
from kiro_crew.apps.market_client import HttpMarketClient, cbor_decode, cbor_encode
from kiro_crew.apps.marketplace_install import MarketInstallError

STORE = "team-store"
OTHER = "other-store"
REGISTRIES = "/api/apps/market/registries"
LISTINGS = "/api/apps/market/listings"
LISTING = "/api/apps/market/listing"
INSTALL = "/api/apps/market/install"


def _view(ref: str = "acme/board", **extra: Any) -> dict[str, Any]:
    ns, _, name = ref.partition("/")
    view = {
        "listingId": f"id-{name}",
        "ref": ref,
        "kind": "app",
        "status": "LISTED",
        "displayName": name.title(),
        "summary": f"{name} summary",
        "category": "productivity",
        "tags": ["a", 7],
        "namespace": ns,
        "name": name,
        "owner": {"kind": "person", "display": ns},
        "latestVersion": 2,
        "latestSemver": "1.1.0",
        "downloads": 0,
        "ratingCount": 1,
        "trust": "COMMUNITY",
        "canPublish": False,
    }
    view.update(extra)
    return view


class FakeReader:
    def __init__(self, pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.pages = pages if pages is not None else [[_view()]]
        self.list_calls = 0
        self.fail: MarketInstallError | None = None

    def list_listings(self, *, kind: str = "app", cursor: str | None = None, limit: int = 100):
        assert kind == "app" and limit == mr.PAGE_LIMIT
        if self.fail is not None:
            raise self.fail
        index = int(cursor or 0)
        self.list_calls += 1
        nxt = str(index + 1) if index + 1 < len(self.pages) else None
        return self.pages[index], nxt

    def get_listing(self, ref: str) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return _view(ref, downloads=5)


class FakeProvider:
    def __init__(self, offers: list[dict[str, str]] | None = None) -> None:
        self._offers = (
            offers
            if offers is not None
            else [
                {"id": STORE, "label": "Team store", "web": "https://store.example"},
                {"id": OTHER, "label": "Other store"},
            ]
        )
        self.state: dict[str, Any] = {"state": "signed_in", "account": "alice"}
        self.reader = FakeReader()
        self.signed: list[tuple[str, str]] = []

    def offers(self) -> list[dict[str, str]]:
        return self._offers

    def status(self, registry_id: str) -> dict[str, Any]:
        return dict(self.state)

    def client(self, registry_id: str) -> Any:
        if self.state["state"] != "signed_in":
            raise MarketInstallError("market_signed_out", "sign in first")
        return self.reader

    def sign_in(self, registry_id: str) -> dict[str, Any]:
        self.signed.append(("in", registry_id))
        return {
            "state": "pending",
            "code": "ABCD",
            "approvalPage": "https://store.example/approve",
            "token": "never-forwarded",
        }

    def sign_out(self, registry_id: str) -> dict[str, Any]:
        self.signed.append(("out", registry_id))
        return {"state": "signed_out"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _setup_env(tmp_path, monkeypatch)


def _write_added(home: Path, ids: list[str]) -> None:
    cfg = json.loads((home / "config.json").read_text())
    cfg["market_registries"] = ids
    (home / "config.json").write_text(json.dumps(cfg))


def _make(
    provider: Any = None, *, user: str | None = "owner", app_token: str = ""
) -> web.Application:
    from types import SimpleNamespace

    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_token
        if user is not None:
            request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    # Routes first, provider after: the order the gateway runs them in.
    routes_mod.register_app_routes(app)
    if provider is not None:
        app[mr.MARKET_REGISTRIES_KEY] = provider
    return app


# ---------------------------------------------------------------------------
# No provider: every surface is absent, nothing opens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_provider_the_read_is_empty_and_the_rest_answer_503(home: Path) -> None:
    _write_added(home, [STORE])  # a hand-edited id changes nothing without a provider
    app = _make()
    async with TestClient(TestServer(app)) as http:
        got = await http.get(REGISTRIES)
        assert got.status == 200
        assert await got.json() == {"offers": [], "added": [], "status": {}}
        for method, path, body in [
            ("PUT", REGISTRIES, {"added": [STORE]}),
            ("POST", f"{REGISTRIES}/{STORE}/signin", {}),
            ("POST", f"{REGISTRIES}/{STORE}/signout", {}),
            ("GET", f"{LISTINGS}?registry={STORE}", None),
            ("GET", f"{LISTING}?registry={STORE}&ref=acme/board", None),
        ]:
            resp = await http.request(method, path, json=body)
            assert resp.status == 503, path
            assert (await resp.json())["code"] == "market_registry_unavailable"
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert install.status == 503
        assert (await install.json())["code"] == "market_client_unavailable"
    assert "market_client_factory" not in app


@pytest.mark.asyncio
async def test_startup_sets_the_factory_when_a_provider_arrives_after_the_routes(
    home: Path,
) -> None:
    app = _make(FakeProvider())
    assert "market_client_factory" not in app  # not at registration time
    async with TestClient(TestServer(app)):
        assert callable(app["market_client_factory"])


# ---------------------------------------------------------------------------
# The owner gate
# ---------------------------------------------------------------------------


_ALL_ROUTES = [
    ("GET", REGISTRIES, None),
    ("PUT", REGISTRIES, {"added": [STORE]}),
    ("POST", f"{REGISTRIES}/{STORE}/signin", {}),
    ("POST", f"{REGISTRIES}/{STORE}/signout", {}),
    ("GET", f"{LISTINGS}?registry={STORE}", None),
    ("GET", f"{LISTING}?registry={STORE}&ref=acme/board", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["stranger", "app-token", "anonymous"])
async def test_every_route_refuses_anyone_but_the_owner(home: Path, who: str) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    kwargs: dict[str, Any] = {
        "stranger": {"user": "someone-else"},
        "app-token": {"app_token": "board"},
        "anonymous": {"user": None},
    }[who]
    async with TestClient(TestServer(_make(provider, **kwargs))) as http:
        for method, path, body in _ALL_ROUTES:
            resp = await http.request(method, path, json=body)
            assert resp.status in (401, 403), (path, resp.status)
    assert provider.signed == []
    assert provider.reader.list_calls == 0


# ---------------------------------------------------------------------------
# The one check: added AND offered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("added", [[], ["not-offered"]], ids=["not-added", "not-offered"])
async def test_a_store_not_added_or_not_offered_is_503_everywhere(
    home: Path, added: list[str]
) -> None:
    _write_added(home, added)
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        for registry in (STORE, "not-offered"):
            for method, path, body in [
                ("POST", f"{REGISTRIES}/{registry}/signin", {}),
                ("GET", f"{LISTINGS}?registry={registry}", None),
                ("GET", f"{LISTING}?registry={registry}&ref=acme/board", None),
            ]:
                resp = await http.request(method, path, json=body)
                assert resp.status == 503, path
                assert (await resp.json())["code"] == "market_registry_unavailable"
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert install.status == 503
        assert (await install.json())["code"] == "market_registry_unavailable"
    assert provider.signed == [] and provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_get_reports_offers_added_and_status(home: Path) -> None:
    _write_added(home, [STORE, "gone"])
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        body = await (await http.get(REGISTRIES)).json()
    assert [o["id"] for o in body["offers"]] == [STORE, OTHER]
    assert body["offers"][1]["web"] == ""
    assert body["added"] == [STORE]
    assert body["status"] == {STORE: {"state": "signed_in", "account": "alice"}}


@pytest.mark.asyncio
async def test_put_refuses_an_id_the_provider_does_not_offer(home: Path) -> None:
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        resp = await http.put(REGISTRIES, json={"added": ["made-up"]})
        assert resp.status == 400
        assert (await resp.json())["code"] == "market_registry_unknown"
        bad = await http.put(REGISTRIES, json={"added": "team-store"})
        assert bad.status == 400
    assert "market_registries" not in json.loads((home / "config.json").read_text())


@pytest.mark.asyncio
async def test_put_saves_the_ids_and_removing_one_drops_its_cache(home: Path) -> None:
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        resp = await http.put(REGISTRIES, json={"added": [STORE, STORE]})
        assert (await resp.json()) == {"ok": True, "added": [STORE]}
        assert json.loads((home / "config.json").read_text())["market_registries"] == [STORE]
        await http.get(f"{LISTINGS}?registry={STORE}")
        assert mr.listing_cache(app).get(STORE, "alice") is not None
        await http.put(REGISTRIES, json={"added": []})
        assert mr.listing_cache(app).get(STORE, "alice") is None
        # Removed: no new install either.
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert (await install.json())["code"] == "market_registry_unavailable"


# ---------------------------------------------------------------------------
# Sign-in, sign-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_in_takes_an_empty_body_and_answers_only_known_fields(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        refused = await http.post(f"{REGISTRIES}/{STORE}/signin", json={"flow": "auto"})
        assert refused.status == 400
        assert provider.signed == []
        resp = await http.post(f"{REGISTRIES}/{STORE}/signin", json={})
        body = await resp.json()
    assert body == {
        "ok": True,
        "registry": STORE,
        "state": "pending",
        "code": "ABCD",
        "approvalPage": "https://store.example/approve",
    }
    assert provider.signed == [("in", STORE)]


def test_an_approval_page_that_is_not_https_is_dropped() -> None:
    assert mr.sign_in_view({"state": "pending", "approvalPage": "javascript:alert(1)"}) == {
        "state": "pending"
    }


@pytest.mark.asyncio
async def test_sign_out_drops_the_cached_rows(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        assert mr.listing_cache(app).get(STORE, "alice") is not None
        await http.post(f"{REGISTRIES}/{STORE}/signout", json={})
    assert mr.listing_cache(app).entries == {}
    assert provider.signed == [("out", STORE)]


# ---------------------------------------------------------------------------
# The install factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_factory_refuses_while_signed_out(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.state = {"state": "needs_sign_in"}
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        resp = await http.post(INSTALL, json={"ref": "acme/board"})
        assert resp.status == 503
        assert (await resp.json())["code"] == "market_signed_out"


@pytest.mark.asyncio
async def test_the_factory_refuses_two_added_stores(home: Path) -> None:
    _write_added(home, [STORE, OTHER])
    app = _make(FakeProvider())
    async with TestClient(TestServer(app)):
        with pytest.raises(MarketInstallError) as err:
            app["market_client_factory"]()
    assert err.value.code == "market_registry_unavailable"


@pytest.mark.asyncio
async def test_the_factory_returns_the_store_client(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)):
        assert app["market_client_factory"]() is provider.reader


# ---------------------------------------------------------------------------
# Listing rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_rows_are_cards_with_no_download_count(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader = FakeReader(
        [[_view(rating={"average": 4.3, "count": 3}), _view("acme/skill-x", kind="skill")]]
    )
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["status"]["state"] == "signed_in"
    assert body["truncated"] is False
    [row] = body["rows"]  # a non-app listing is not a card
    assert row["ref"] == "acme/board" and row["registry"] == STORE
    assert row["tags"] == ["a"]
    assert row["downloads"] == {"status": "absent"}
    assert row["rating"] == {"status": "ok", "value": 4.3}
    assert row["ratingCount"] == {"status": "ok", "value": 1}
    assert row["installed"] is False and row["nameTaken"] is False


@pytest.mark.asyncio
async def test_a_signed_out_store_lists_nothing_and_says_so(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.state = {"state": "needs_sign_in"}
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == [] and body["status"]["state"] == "needs_sign_in"
    assert provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_paging_stops_at_ten_pages_and_marks_truncated(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader = FakeReader([[_view(f"acme/app-{i}")] for i in range(12)])
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert provider.reader.list_calls == mr.MAX_PAGES
    assert len(body["rows"]) == mr.MAX_PAGES
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_the_cache_is_per_account_and_refresh_drops_it(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        await http.get(f"{LISTINGS}?registry={STORE}")
        assert provider.reader.list_calls == 1
        provider.state = {"state": "signed_in", "account": "bob"}
        await http.get(f"{LISTINGS}?registry={STORE}")
        assert provider.reader.list_calls == 2
        await http.get(f"{LISTINGS}?registry={STORE}&refresh=1")
        assert provider.reader.list_calls == 3


def test_the_cache_expires_after_an_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(mr, "_now", lambda: clock[0])
    cache = mr.ListingCache()
    cache.put(STORE, "alice", [_view()], False)
    clock[0] += mr.LISTING_CACHE_TTL_S - 1
    assert cache.get(STORE, "alice") is not None
    clock[0] += 2
    assert cache.get(STORE, "alice") is None


@pytest.mark.asyncio
async def test_a_refused_read_reports_the_code_and_a_fresh_status(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader.fail = MarketInstallError("market_api_refused", "ListListings answered 403")
    statuses = iter([{"state": "signed_in", "account": "alice"}, {"state": "no_access"}])
    provider.status = lambda _id: next(statuses)  # type: ignore[method-assign]

    async with TestClient(TestServer(_make(provider))) as http:
        first = await http.get(f"{LISTINGS}?registry={STORE}")
        assert first.status == 200
        body = await first.json()
    assert body["status"] == {"state": "no_access", "account": ""}
    assert body["error"] == {"code": "market_api_refused"}
    assert body["rows"] == []
    assert "403" not in json.dumps(body)


def test_same_name_from_another_source_is_its_own_card_marked_name_taken() -> None:
    installed = {"board": {"name": "board", "source": "registry:board"}}
    row = mr.listing_row(_view(), STORE, installed)
    assert row is not None
    assert row["installed"] is False and row["nameTaken"] is True
    mine = {"board": {"name": "board", "source": "market:acme/board@2"}}
    row = mr.listing_row(_view(), STORE, mine)
    assert row is not None and row["installed"] is True and row["installedVersion"] == 2
    other_ns = {"board": {"name": "board", "source": "market:someone/board@2"}}
    row = mr.listing_row(_view(), STORE, other_ns)
    assert row is not None and row["installed"] is False and row["nameTaken"] is True


@pytest.mark.asyncio
async def test_the_detail_route_answers_one_listing_with_downloads(home: Path) -> None:
    _write_added(home, [STORE])
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        bad = await http.get(f"{LISTING}?registry={STORE}&ref=../x")
        assert bad.status == 400
        body = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
    assert body["listing"]["ref"] == "acme/board"
    assert body["listing"]["downloads"] == {"status": "ok", "value": 5}


def test_detail_downloads_unavailable_is_not_zero() -> None:
    row = mr.listing_row(_view(downloadsUnavailable=True), STORE, {}, detail=True)
    assert row is not None and row["downloads"] == {"status": "unavailable"}


# ---------------------------------------------------------------------------
# HttpMarketClient: ListListings and GetListing over CBOR
# ---------------------------------------------------------------------------


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> HttpMarketClient:
    real = httpx.Client

    def _client(**kwargs: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(market_client_mod.httpx, "Client", _client)
    return HttpMarketClient("https://store.example", token=lambda: "tok")


def test_list_listings_and_get_listing_speak_cbor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[1]
        body = cbor_decode(request.content)
        seen.append((op, body))
        assert request.headers["authorization"] == "Bearer tok"
        if op == "ListListings":
            answer: dict[str, Any] = {"items": [_view(), "junk"], "nextCursor": "c2"}
        else:
            answer = {"listing": _view(body["ref"])}
        return httpx.Response(200, content=cbor_encode(answer))

    client = _mock_client(monkeypatch, handler)
    items, cursor = client.list_listings(kind="app", cursor="c1", limit=100)
    assert [i["ref"] for i in items] == ["acme/board"] and cursor == "c2"
    assert client.get_listing("acme/notes")["ref"] == "acme/notes"
    assert seen[0] == ("ListListings", {"kind": "app", "limit": 100, "cursor": "c1"})
    assert seen[1] == ("GetListing", {"ref": "acme/notes"})


def test_a_listing_answer_without_items_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _mock_client(
        monkeypatch, lambda r: httpx.Response(200, content=cbor_encode({"nothing": 1}))
    )
    with pytest.raises(MarketInstallError) as err:
        client.list_listings()
    assert err.value.code == "market_api_invalid"
    with pytest.raises(MarketInstallError):
        client.get_listing("acme/board")


# ---------------------------------------------------------------------------
# Catalog rows do not claim a Kiro Space install
# ---------------------------------------------------------------------------


def test_catalog_rows_give_no_update_over_a_market_install() -> None:
    from kiro_crew.apps.registry_pipeline.catalog import _enrich_with_install_status

    rows = [{"name": "board", "version": "9.0.0"}, {"name": "notes", "version": "9.0.0"}]
    installed = {
        "board": {"name": "board", "version": "1.0.0", "source": "market:acme/board@1"},
        "notes": {"name": "notes", "version": "1.0.0", "source": "registry:notes"},
    }
    board, notes = _enrich_with_install_status(rows, installed)
    assert board["installed"] is True
    assert board["updateAvailable"] is False and board["installedFrom"] == "market"
    assert notes["updateAvailable"] is True and "installedFrom" not in notes


def test_update_notes_skip_a_market_install(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.apps.registry_pipeline.catalog import _enrich_with_install_status
    from kiro_crew.apps.update_notify import prepare_app_update_notes

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    installed = {"board": {"name": "board", "version": "1.0.0", "source": "market:acme/board@1"}}
    first = _enrich_with_install_status([{"name": "board", "version": "1.0.0"}], installed)
    prepare_app_update_notes(first)  # seeds the ledger
    later = _enrich_with_install_status([{"name": "board", "version": "2.0.0"}], installed)
    assert prepare_app_update_notes(later) == []


# ---------------------------------------------------------------------------
# The registry install routes refuse a Kiro Space install (no clone, no change)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["install", "install-stream"])
async def test_registry_install_routes_refuse_a_market_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    from apps_routes_coverage_helpers import APP, _make_app

    from kiro_crew.apps.manager import _read_installed, install_app
    from kiro_crew.apps.registry_pipeline import install as install_mod

    _setup_env(tmp_path, monkeypatch)
    source = f"market:acme/{APP}@3"
    assert install_app(
        _make_app_source(tmp_path), expected_name=APP, market_source=source, source_commit="c" * 64
    ).ok
    before = _read_installed(APP)
    touched: list[str] = []

    def _no(*_a: Any, **_k: Any) -> Any:
        touched.append("touched")
        raise AssertionError("must not resolve or clone")

    monkeypatch.setattr(install_mod, "_resolve_install_entry", _no)
    monkeypatch.setattr(install_mod, "_clone_build_app", _no)
    monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())

    path = "/api/apps/registry/install-stream" if stream else "/api/apps/registry/install"
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as http:
        resp = await http.post(path, json={"name": APP})
        text = await resp.text()
    if stream:
        assert resp.status == 200
        done = [
            json.loads(line[len("data: ") :])
            for block in text.split("\n\n")
            if "event: done" in block
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        assert done and done[-1]["ok"] is False
        assert done[-1]["code"] == "app_update_market_source"
    else:
        assert resp.status == 400
        assert json.loads(text)["code"] == "app_update_market_source"
    assert touched == []
    assert _read_installed(APP) == before


# ---------------------------------------------------------------------------
# The sign-in folder is masked in every sandbox mode
# ---------------------------------------------------------------------------


def test_the_sign_in_folder_is_in_all_three_mask_tables() -> None:
    from kiro_crew import sandbox, security

    leaf = mr.MARKET_REGISTRIES_LEAF
    assert leaf in sandbox._CREW_HIDDEN_LEAVES
    assert leaf in security._CREW_SECRET_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    for prefix in (".kiro/crew", ".kirocrew"):
        assert security.is_sensitive_path(f"~/{prefix}/{leaf}/{STORE}/credentials.json")


def test_the_data_dir_follows_kirocrew_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    got = mr.registry_data_dir(STORE)
    assert got == (tmp_path / "home" / mr.MARKET_REGISTRIES_LEAF / STORE)
    assert not got.exists()
    with pytest.raises(ValueError):
        mr.registry_data_dir("../x")


def test_config_round_trips_market_registries(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"market_registries": [STORE, 3, STORE]}))
    cfg = KiroCrewConfig.load()
    assert cfg.market_registries == [STORE]
    assert cfg.to_dict()["market_registries"] == [STORE]
