"""The ``browser`` tool's URL gate: public http(s), or a loopback origin the operator listed.

One module, two callers. The ``browser`` MCP tool (``mcp_tools/browser.py``, the
stdio shim) applies :func:`url_allowed` to the URL an op is ASKED to open, before
the op leaves the shim. The gateway (``browser_harness/ops.py``, through
``OpContext.url_allowed``) applies the same predicate to the URL the navigation
actually COMMITTED on, after every redirect: a public page that answers 302 to
``http://127.0.0.1:<port>/`` or a link-local address would otherwise be read back
through a browser that may be the person's logged-in Chrome, one hop past the
gate. DNS rebinding (a public name resolving to a private address) is the one
residual neither check can see without resolving names, and stays documented.

The operator's site lists (``browser.sites.allow`` / ``browser.sites.deny``) only
NARROW this gate, at both call sites: :func:`site_rule` names the entry that
refuses a URL, and with both lists empty it refuses nothing, so the gate is
exactly the public/loopback rule above.

Imports nothing from the dashboard or ``mcp_core``, so the shim can import it
without pulling the gateway in; the gateway's own port is a parameter.
"""

from __future__ import annotations

import ipaddress
from typing import Any, NamedTuple
from urllib.parse import unquote_to_bytes, urlsplit

from kiro_crew.browser_harness.credentials import domain_allowed
from kiro_crew.security import canonicalize_ip

_LOOPBACK_HOST_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}

# Code points a browser refuses in a host once it is percent-decoded.
_FORBIDDEN_DECODED_HOST_CHARS = frozenset("%/@?#[]:\\<>^| ")


def _percent_decoded_host(host: str) -> str | None:
    """Decode ``host`` once, as the browser's URL parser does.

    ``urlsplit`` keeps ``%31%32%37.0.0.1`` encoded, but the browser decodes it
    to ``127.0.0.1``. Returns None when the browser would reject the decoded
    host (invalid UTF-8, or a char a host may not carry).
    """
    try:
        decoded = unquote_to_bytes(host).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        ch in _FORBIDDEN_DECODED_HOST_CHARS or ord(ch) < 0x21 or ord(ch) == 0x7F for ch in decoded
    ):
        return None
    return decoded


def _browser_host(url: str) -> str | None:
    """The host the browser would open for *url*, lowercased; None when it is refused.

    Refused: a backslash, a space or any control char anywhere in the URL (the
    parser differential :func:`target_is_public` describes), a scheme other than
    http(s), and a percent-encoded host the browser would reject once decoded. A
    percent-encoded host is otherwise decoded as the browser decodes it. ``""``
    is a URL with no host.
    """
    raw = url.strip()
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = parts.hostname or ""
    if "%" in host and ":" not in host:  # bracketed IPv6 keeps its own path
        decoded = _percent_decoded_host(host)
        if decoded is None:
            return None
        host = decoded
    return host.lower().rstrip(".")


def target_is_public(url: str) -> bool:
    """True iff ``url`` is an ordinary public http(s) page.

    The browser accepts ANY http(s) URL, so without this a ``navigate`` op could
    drive a loopback control plane (this dashboard included) or the
    ``169.254.169.254`` metadata endpoint. Kept LOCAL (not imported from the
    dashboard) so the ``kirocrew-core`` shim stays free of dashboard deps.

    ``is_global`` subsumes loopback/link-local/private/CGNAT/reserved in one
    predicate, applied to the address and any embedded IPv4 (``ipv4_mapped`` /
    ``sixtofour``). The host is first run through ``security.canonicalize_ip`` so
    an alternate IPv4 encoding the browser would still normalize -- decimal
    (``2852039166``), hex (``0x...``), octal, short/mixed, IPv6-mapped -- is
    resolved to dotted-quad and caught rather than mistaken for a DNS name. DNS
    names are NOT resolved (a blocking call + rebinding TOCTOU on the hot path);
    the residual public-name->private-IP risk is accepted and documented in the
    prompt.

    A percent-encoded host (``%31%32%37.0.0.1``, ``loc%61lhost``) is decoded
    first, as the browser does, so every check below sees the browser's host.
    """
    # Parser-differential SSRF (refused in :func:`_browser_host`): reject backslash (Chromium treats it as '/'),
    # and any control/whitespace char (tab/CR/LF and the rest), which browsers
    # strip or normalize but urlsplit does not -- e.g.
    # ``http://169.254.169.254\@example.com/`` parses to host ``example.com``
    # here yet Chromium navigates to the metadata endpoint. A legitimate URL
    # needs none of these (percent-encode instead), so refuse rather than parse.
    host = _browser_host(url)
    if host is None:
        return False
    if not host or host in _LOOPBACK_HOST_NAMES or host.endswith(".localhost"):
        return False
    # Reject non-ASCII hosts: a fullwidth-digit / IDN form the browser may
    # normalize toward a private IP would otherwise slip through the DNS branch
    # below. Legitimate IDN sites are reached by their ASCII punycode (xn--...).
    if any(ord(ch) > 0x7F for ch in host):
        return False
    try:
        addr: Any = ipaddress.ip_address(canonicalize_ip(host))
    except ValueError:
        return True  # a DNS name (not an IP in any encoding); public browsing unaffected
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def target_is_allowed_local(url: str, allowed: frozenset[str], gateway_port: int | None) -> bool:
    """True iff ``url`` is a loopback origin the operator listed, exactly.

    The exception to :func:`target_is_public`, never a relaxation of
    it: the same parser-differential refusals run first, then the URL's own
    ``host:port`` (default port filled in from the scheme) must equal an entry
    byte for byte. Only the three spellings ``config.sections`` admits can ever
    match, so an alternate IPv4 encoding (``0x7f000001``, ``2130706433``) or a
    ``*.localhost`` name stays refused even when ``localhost:<port>`` is listed.
    Userinfo is refused outright so ``http://localhost:5173@host/`` cannot be
    read two ways. The gateway's own port (*gateway_port*) is refused even when
    listed: it is the control plane this gate exists to keep the browser away
    from; an unknown port refuses every local target.
    """
    if not allowed:
        return False
    raw = url.strip()
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if scheme == "https" else 80
    if f"{host}:{port}" not in allowed:
        return False
    if gateway_port is None:
        return False  # gateway port unknowable -> cannot prove this is not it
    return port != gateway_port


class SiteLists(NamedTuple):
    """``browser.sites``: the operator's allow and deny entries, already normalized.

    ``unread`` means the config read could not see the lists, so nobody knows what
    they hold: :func:`site_rule` then refuses every page.
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    unread: bool = False


#: No lists: :func:`site_rule` refuses nothing.
NO_SITE_LISTS = SiteLists()

#: Lists the config read could not see: :func:`site_rule` refuses every page, because
#: an empty answer would lift whatever limit the unreadable file sets.
UNREAD_SITE_LISTS = SiteLists(unread=True)

_UNREAD_RULE = (
    "the site lists could not be read (the config file, its browser section or "
    "browser.sites did not parse), so no page opens until the user fixes it and "
    "restarts Kiro Crew"
)


def _site_key(host: str) -> str:
    """*host* as entries compare it: an IP in any encoding (``0x7f000001``, ``[::1]``)
    as its canonical text, so both sides of a match meet; a name unchanged."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return str(ipaddress.ip_address(canonicalize_ip(bare)))
    except ValueError:
        return host


def _entry_covers(entry: str, key: str) -> bool:
    """``example.com`` is that host exactly; ``*.example.com`` is it and every name
    under it on a whole-label boundary (the sign-in seam's ``domain_allowed``)."""
    if entry.startswith("*."):
        return domain_allowed(key, (entry[2:],))
    return key == _site_key(entry)


def site_rule(url: Any, sites: SiteLists) -> str | None:
    """The site-list rule that refuses *url*, or None when the lists admit it.

    A deny entry always wins; a non-empty allow list admits only a host it covers;
    both empty refuse nothing; lists the config read could not see refuse every
    page. The text names the setting and its entry, never anything the page wrote.
    """
    if sites.unread:
        return _UNREAD_RULE
    if not sites.allow and not sites.deny:
        return None
    host = _browser_host(url) if isinstance(url, str) else None
    if not host:
        return "its address has no host the site lists can check"
    key = _site_key(host)
    for entry in sites.deny:
        if _entry_covers(entry, key):
            return f"it matches the entry {entry} in browser.sites.deny"
    if sites.allow and not any(_entry_covers(entry, key) for entry in sites.allow):
        return "its host is not covered by any entry in browser.sites.allow"
    return None


def site_lists_of(browser_cfg: Any) -> SiteLists:
    """The :class:`SiteLists` of a loaded ``browser`` section, duck-typed so this file
    imports no config module when it loads; a section without ``sites`` yields no lists."""
    sites = getattr(browser_cfg, "sites", None)

    def _entries(name: str) -> tuple[str, ...]:
        raw = getattr(sites, name, None)
        return tuple(e for e in raw if isinstance(e, str)) if isinstance(raw, list) else ()

    return SiteLists(allow=_entries("allow"), deny=_entries("deny"))


def site_lists_of_config(cfg: Any) -> SiteLists:
    """The :class:`SiteLists` of a loaded config, or :data:`UNREAD_SITE_LISTS` when the
    load could not read them (``config.resolution.browser_site_lists_unknown``).

    Asks the process's sticky record as well as the load's own marks, as
    ``observed_degraded_sections`` advises for a gate: it is the one answer every
    config path agrees on.
    """
    from kiro_crew.config.resolution import (
        browser_site_lists_unknown,
        observed_degraded_sections,
    )

    degraded = frozenset(getattr(cfg, "degraded_sections", frozenset()))
    if browser_site_lists_unknown(degraded | observed_degraded_sections()):
        return UNREAD_SITE_LISTS
    return site_lists_of(getattr(cfg, "browser", None))


def url_allowed(
    url: Any,
    allowed_local: frozenset[str],
    gateway_port: int | None,
    sites: SiteLists = NO_SITE_LISTS,
) -> bool:
    """The navigate gate: public, or an operator-listed loopback origin (never the
    gateway's), and admitted by the operator's site lists (:func:`site_rule`)."""
    if not isinstance(url, str):
        return False
    if not (target_is_public(url) or target_is_allowed_local(url, allowed_local, gateway_port)):
        return False
    return site_rule(url, sites) is None


__all__ = [
    "NO_SITE_LISTS",
    "SiteLists",
    "UNREAD_SITE_LISTS",
    "site_lists_of",
    "site_lists_of_config",
    "site_rule",
    "target_is_allowed_local",
    "target_is_public",
    "url_allowed",
]
