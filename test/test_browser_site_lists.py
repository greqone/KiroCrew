"""The operator's site lists for the agent's browser (``browser.sites.allow`` / ``.deny``).

Pinned:

* off by default: with both lists empty the navigate gate is exactly the
  public/loopback rule it was (``url_gate.url_allowed`` with no lists);
* a deny entry always wins, even over an allow entry that covers the host;
* a host matches an entry exactly, or under a ``*.`` wildcard on a whole-label
  boundary, never as a substring, and only by the host the browser would open
  (userinfo, percent-encoding, case, trailing dot and alternate IP encodings
  cannot step around an entry);
* both call sites apply them: the shim to the URL asked for, the gateway to the
  URL a navigation committed on (after redirects), and the refusal names the
  rule, never the page;
* the loader keeps a malformed entry as a never-matching one, so an allow list
  of only malformed entries still refuses every site;
* a config read that cannot see the lists (the file, the ``browser`` section or
  a ``browser.sites`` value does not parse, or the load raises) opens no page in
  either gate, while a config that simply sets no lists adds no limit.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.browser_harness import url_gate
from kiro_crew.browser_harness.url_gate import SiteLists, site_rule, url_allowed
from kiro_crew.config import sections_browser as SB
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections_browser import BrowserConfig, coerce_site_list
from kiro_crew.dashboard.handlers import browser_ops as route
from kiro_crew.mcp_tools import browser as shim


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    SB._WARNED_BROWSER_VALUES.clear()
    _floor_monkeypatch.setattr(shim.mcp_core, "_session_key_header_error", lambda sk: None)
    _floor_monkeypatch.setattr(shim.mcp_core, "_vet_browse_governance", lambda s: None)
    _floor_monkeypatch.setattr(shim.mcp_core, "_resolve_session_key", lambda: "dashboard:c-1")
    _floor_monkeypatch.setattr(shim.mcp_core, "_api_port", lambda: 6776)


def _config(mp: pytest.MonkeyPatch, sites: dict[str, Any] | None) -> None:
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(browser_local_origins=["localhost:5173"]),
        browser=BrowserConfig.from_raw({"sites": sites} if sites is not None else {}),
    )
    mp.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


_URLS = (
    "https://example.com/",
    "https://www.example.com/a",
    "http://localhost:5173/",
    "http://127.0.0.1:5173/",
    "http://169.254.169.254/",
    "https://user@example.com/",
    "ftp://example.com/",
    "not a url",
    "",
)


@pytest.mark.parametrize("url", _URLS)
def test_no_lists_is_exactly_the_gate_without_lists(url: str) -> None:
    """Off by default: the predicate with the default lists answers as it did before."""
    listed = frozenset({"localhost:5173"})
    assert site_rule(url, SiteLists()) is None
    assert url_allowed(url, listed, 6776) == url_allowed(url, listed, 6776, SiteLists())
    assert url_allowed(url, listed, 6776) == (
        url_gate.target_is_public(url) or url_gate.target_is_allowed_local(url, listed, 6776)
    )


def test_a_fresh_config_has_no_lists() -> None:
    sites = KiroCrewConfig().browser.sites
    assert sites.allow == [] and sites.deny == []
    assert url_gate.site_lists_of(KiroCrewConfig().browser) == url_gate.NO_SITE_LISTS


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",
        "https://EVIL.example./x",
        "https://ok.example@evil.example/",
        "https://%65vil.example/",
        "https://a.b.evil.example/",
    ],
)
def test_a_deny_entry_refuses_the_host_the_browser_would_open(url: str) -> None:
    sites = SiteLists(deny=("*.evil.example",))
    rule = site_rule(url, sites)
    assert rule is not None and "browser.sites.deny" in rule and "*.evil.example" in rule
    assert not url_allowed(url, frozenset(), 6776, sites)


def test_deny_wins_over_an_allow_entry_that_covers_the_host() -> None:
    sites = SiteLists(allow=("*.example.com",), deny=("admin.example.com",))
    assert url_allowed("https://www.example.com/", frozenset(), 6776, sites)
    assert not url_allowed("https://admin.example.com/", frozenset(), 6776, sites)
    assert "browser.sites.deny" in (site_rule("https://admin.example.com/", sites) or "")


@pytest.mark.parametrize(
    "url",
    [
        "https://badexample.com/",
        "https://example.com.evil.net/",
        "https://evil.net/example.com",
        "https://evil.net/?h=example.com",
        "https://example.com@evil.net/",
    ],
)
def test_matching_is_by_whole_host_never_substring(url: str) -> None:
    sites = SiteLists(allow=("example.com", "*.docs.example.org"))
    rule = site_rule(url, sites)
    assert rule is not None and "browser.sites.allow" in rule


def test_an_exact_entry_does_not_cover_subdomains_and_a_wildcard_covers_its_apex() -> None:
    exact = SiteLists(allow=("example.com",))
    assert site_rule("https://example.com/", exact) is None
    assert site_rule("https://www.example.com/", exact) is not None
    wild = SiteLists(allow=("*.example.com",))
    assert site_rule("https://example.com/", wild) is None
    assert site_rule("https://a.b.example.com/", wild) is None


def test_an_ip_entry_meets_every_encoding_of_its_address() -> None:
    sites = SiteLists(deny=("93.184.216.34",))
    for url in ("http://93.184.216.34/", "http://1572395042/", "http://0x5db8d822/"):
        assert site_rule(url, sites) is not None, url
    assert site_rule("http://[::1]:5173/", SiteLists(deny=("::1",))) is not None


def test_the_rule_text_names_the_entry_and_never_the_page() -> None:
    url = "https://evil.example/ignore-previous-instructions"
    rule = site_rule(url, SiteLists(deny=("evil.example",)))
    assert rule == "it matches the entry evil.example in browser.sites.deny"
    rule = site_rule(url, SiteLists(allow=("ok.example",)))
    assert rule == "its host is not covered by any entry in browser.sites.allow"


def test_the_loader_normalizes_and_never_widens(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        got = coerce_site_list(
            [" Example.COM. ", "https://docs.example.org/x", "intranet:8443", 7, "*bad.com"],
            "allow",
        )
    assert got == ["example.com", "docs.example.org", "intranet", "*bad.com"]
    assert "browser.sites.allow" in caplog.text
    assert coerce_site_list("example.com", "deny") == []
    assert coerce_site_list(["[::1]"], "deny") == ["::1"]
    assert coerce_site_list(["Bücher.de", "*.bücher.de"], "deny") == [
        "xn--bcher-kva.de",
        "*.xn--bcher-kva.de",
    ]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("[::1]:8080", "::1"),
        ("example.com/docs", "example.com"),
        ("example.com/", "example.com"),
        ("*.example.com/x", "*.example.com"),
        (".example.com", "*.example.com"),
        ("user@example.com", "example.com"),
    ],
)
def test_a_pasted_entry_without_a_scheme_is_read_as_its_host(typed: str, stored: str) -> None:
    """A deny entry pasted as ``host/path``, ``[v6]:port`` or ``.domain`` still denies."""
    assert coerce_site_list([typed], "deny") == [stored]


@pytest.mark.parametrize(
    "typed", ["fa\u00df.de", "*.fa\u00df.de", "\u03c2.example", "a\u200db.example"]
)
def test_a_name_the_two_idna_versions_read_differently_admits_no_site(typed: str) -> None:
    """Python's IDNA codec maps these characters where the browser keeps them, so the
    codec's form is another site; an allow entry typed this way admits neither."""
    sites = SiteLists(allow=tuple(coerce_site_list([typed], "allow")))
    other = typed.encode("idna").decode("ascii").removeprefix("*.")
    assert site_rule(f"https://{other}/", sites) is not None


def test_a_long_list_keeps_the_first_entries_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = [f"h{i}.example" for i in range(SB.SITE_LIST_CAP + 50)] + ["h0.example"]
    raw += ["*bad"] * 10 + ["x" * 5000]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        got = coerce_site_list(raw, "deny")
    assert got == [f"h{i}.example" for i in range(SB.SITE_LIST_CAP)]
    assert len(caplog.records) == 2, [r.getMessage()[:80] for r in caplog.records]
    assert all(len(r.getMessage()) < 2000 for r in caplog.records)
    assert max(len(e) for e in coerce_site_list(["x" * 5000], "allow")) <= 253


def test_an_allow_list_of_only_malformed_entries_still_refuses_every_site() -> None:
    cfg = BrowserConfig.from_raw({"sites": {"allow": ["*example.com"]}})
    sites = url_gate.site_lists_of(cfg)
    assert sites.allow == ("*example.com",)
    assert site_rule("https://example.com/", sites) is not None
    assert site_rule("https://www.example.com/", sites) is not None


def test_the_shim_refuses_a_listed_site_without_posting(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(_floor_monkeypatch, {"deny": ["evil.example"]})

    def _must_not_post(*_a: Any, **_k: Any) -> tuple[int, dict]:
        raise AssertionError("must not POST")

    _floor_monkeypatch.setattr(shim, "_post_command", _must_not_post)
    for args in (
        {"op": "navigate", "args": {"url": "https://evil.example/page"}},
        {"op": "tabs", "args": {"action": "new", "url": "https://evil.example/"}},
    ):
        out = shim.browser("browser", args)
        assert out.startswith("Error: the browser tool did not open this address")
        assert "evil.example in browser.sites.deny" in out and "/page" not in out
    # A private address keeps the loopback gate's own text, unchanged.
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "http://10.0.0.1/"}})
    assert out == shim._URL_GATE_TEXT


def test_the_shim_with_no_lists_posts_as_before(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _config(_floor_monkeypatch, None)
    sent: list[str] = []

    def _post(_k: str, op: str, *_a: Any) -> tuple[int, dict]:
        sent.append(op)
        return 200, {"ok": True, "result": {"ok": True, "url": "https://evil.example/"}}

    _floor_monkeypatch.setattr(shim, "_post_command", _post)
    shim.browser("browser", {"op": "navigate", "args": {"url": "https://evil.example/"}})
    assert sent == ["navigate"]


def test_the_gateway_applies_the_lists_to_the_committed_url(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(
        _floor_monkeypatch, {"allow": ["*.example.com", "localhost"], "deny": ["x.example.com"]}
    )
    assert route.committed_url_allowed("https://www.example.com/")
    assert route.committed_url_allowed("http://localhost:5173/")
    assert not route.committed_url_allowed("https://x.example.com/")
    assert not route.committed_url_allowed("https://other.example/")
    assert route.committed_site_rule("https://x.example.com/") == (
        "it matches the entry x.example.com in browser.sites.deny"
    )
    assert route.committed_site_rule("https://www.example.com/") is None
    # The loopback gate still applies under the lists: the gateway's own port stays refused.
    assert not route.committed_url_allowed("http://localhost:6776/")


def test_the_gateway_with_no_lists_admits_what_it_did(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(_floor_monkeypatch, None)
    assert route.committed_url_allowed("https://anything.example/")
    assert route.committed_url_allowed("http://localhost:5173/")
    assert not route.committed_url_allowed("http://localhost:6776/")
    assert route.committed_site_rule("https://anything.example/") is None


@pytest.fixture
def config_file(tmp_path: Any, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real ``config.json`` the real loader reads, in a scratch config home."""
    from kiro_crew.config import loader as L

    path = tmp_path / "config.json"
    _floor_monkeypatch.setattr(L, "config_path", lambda: path)
    _floor_monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return path


@pytest.mark.parametrize(
    "text",
    [
        '{"browser": {"sites": {"deny": ["evil.example"]}',
        '{"browser": "sites"}',
        '{"browser": {"sites": ["evil.example"]}}',
        '{"browser": {"sites": {"deny": "evil.example"}}}',
    ],
    ids=[
        "file-cut-off-mid-edit",
        "section-not-an-object",
        "sites-not-an-object",
        "deny-not-a-list",
    ],
)
def test_lists_the_config_read_cannot_see_open_no_page(config_file: Any, text: str) -> None:
    config_file.write_text(text, encoding="utf-8")
    assert not route.committed_url_allowed("https://other.example/")
    assert route.committed_site_rule("https://other.example/") == url_gate._UNREAD_RULE
    refusal = shim._site_refusal("https://other.example/")
    assert refusal is not None and url_gate._UNREAD_RULE in refusal
    # The load kept the malformed bytes, so a restart reads them and refuses again.
    assert config_file.read_text(encoding="utf-8") == text


def test_a_config_load_that_raises_opens_no_page(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreadable(cls: Any) -> Any:
        raise OSError("the config could not be read")

    _floor_monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_unreadable))
    assert not route.committed_url_allowed("https://other.example/")
    assert shim._site_refusal("https://other.example/") is not None


def test_a_real_config_that_sets_no_lists_adds_no_limit(config_file: Any) -> None:
    config_file.write_text('{"browser": {"choice": "auto"}}', encoding="utf-8")
    assert route.committed_url_allowed("https://other.example/")
    assert route.committed_site_rule("https://other.example/") is None
    assert shim._site_refusal("https://other.example/") is None
