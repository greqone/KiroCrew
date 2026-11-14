"""The ``browser`` section: which browser the agent drives, and the live view in chat.

Settings -> Browser writes every key here through ``PATCH /api/config/kirocrew``;
``browser_harness`` (the supervisor and the screencast) reads them with these
defaults. ``dashboard.browser_local_origins`` lives on the ``dashboard`` section;
it is the Preview tab's allowlist AND the loopback exception of the agent's
``browser`` navigate gate (``mcp_tools/browser.py``), so an entry there widens
what the agent may open, not only what the person previews.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass, field
from urllib.parse import urlsplit as _urlsplit

from kiro_crew.config.fields import _meta, _safe_bool, _safe_int
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")


#: Which browser the agent drives. ``auto`` is the discovery order the RFC fixes
#: (the user's Chrome, then Edge on Windows, then Kiro's headless Chromium when
#: there is no display or the Chrome policy forbids remote debugging). The three
#: hand picks each pin one rung of that ladder; ``headless`` is Kiro's Chromium
#: with no window even on a desktop.
BROWSER_CHOICE_AUTO = "auto"
BROWSER_CHOICES = (BROWSER_CHOICE_AUTO, "user_chrome", "kiro_chromium", "headless")

#: Stream quality of the live view. ``auto`` lets the adaptive ladder in
#: ``browser_harness/screencast.py`` pick a rung from the measured link; a fixed
#: value pins the JPEG quality and scale and leaves only the frame rate adaptive.
#: ``auto`` and ``high`` also send one sharp settle frame when the page goes still.
LIVE_QUALITY_AUTO = "auto"
LIVE_QUALITIES = (LIVE_QUALITY_AUTO, "high", "medium", "low")

#: Frame-rate ceilings the card can be asked for. 60 is the ladder's top rung; the
#: machine's own limit is lower on a slow link, and the ladder never exceeds this.
LIVE_MAX_FPS_CHOICES = (15, 30, 60)
LIVE_MAX_FPS_DEFAULT = 60

# Values already warned about in this process, keyed on ``repr`` because they
# arrive from ``config.json`` and may be unhashable. The gateway loads config
# repeatedly, so an unusable stored value is per-install information, not
# per-load. Exposed for tests to reset.
_WARNED_BROWSER_VALUES: set[str] = set()


def _warn_once(key: str, value: object, resolved: object, choices: tuple) -> None:
    seen = f"{key}={value!r}"
    if seen in _WARNED_BROWSER_VALUES:
        return
    _WARNED_BROWSER_VALUES.add(seen)
    logger.warning(
        "Unusable %s %r; using %r instead. Choices: %s. Run 'kirocrew config set %s "
        "<value>' to choose one, or 'kirocrew config defaults --adopt %s' to drop the "
        "stored value and this notice.",
        key,
        value,
        resolved,
        ", ".join(str(c) for c in choices),
        key,
        key,
    )


def browser_choice_resolution(value: object) -> str:
    """The browser ``browser.choice`` of *value* selects. Pure; never logs.

    A JSON ``null`` is the absent key and takes the default. Any value outside
    :data:`BROWSER_CHOICES` degrades to ``auto``: that is the only value that can
    never leave the agent without a browser, since the discovery order falls
    through to Kiro's own Chromium.
    """
    if value in BROWSER_CHOICES:
        return str(value)
    return BROWSER_CHOICE_AUTO


def live_quality_resolution(value: object) -> str:
    """The quality ``browser.live.quality`` of *value* selects. Pure; never logs."""
    if value in LIVE_QUALITIES:
        return str(value)
    return LIVE_QUALITY_AUTO


def live_max_fps_resolution(value: object) -> int:
    """The ceiling ``browser.live.max_fps`` of *value* selects. Pure; never logs.

    A digit string is an accepted spelling (``"30"``), so a value typed at the
    CLI reads back as the int. A bool is NOT a frame rate even though it is an
    int in Python; it degrades like any other junk, to the top rung, which is
    what the ladder would have reached anyway.
    """
    if isinstance(value, bool):
        return LIVE_MAX_FPS_DEFAULT
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and value in LIVE_MAX_FPS_CHOICES:
        return value
    return LIVE_MAX_FPS_DEFAULT


def _validated_choice(value: object) -> str:
    resolved = browser_choice_resolution(value)
    if value is not None and value not in BROWSER_CHOICES:
        _warn_once("browser.choice", value, resolved, BROWSER_CHOICES)
    return resolved


def _validated_quality(value: object) -> str:
    resolved = live_quality_resolution(value)
    if value is not None and value not in LIVE_QUALITIES:
        _warn_once("browser.live.quality", value, resolved, LIVE_QUALITIES)
    return resolved


def _validated_max_fps(value: object) -> int:
    resolved = live_max_fps_resolution(value)
    spelled = int(value.strip()) if isinstance(value, str) and value.strip().isdigit() else value
    if value is not None and not (
        isinstance(spelled, int) and not isinstance(spelled, bool) and spelled == resolved
    ):
        _warn_once("browser.live.max_fps", value, resolved, LIVE_MAX_FPS_CHOICES)
    return resolved


@dataclass
class BrowserLiveConfig:
    """``browser.live``: the live view of the agent's browser in chat."""

    enabled: bool = field(
        default=True,
        metadata=_meta(
            "Show live view in chat",
            "When the agent uses the browser, the turn shows a live card of the page "
            "in the chat. Click the picture to take over. Off hides the card; the "
            "agent still browses.",
        ),
    )
    quality: str = field(
        default=LIVE_QUALITY_AUTO,
        metadata=_meta(
            "Quality",
            "`auto` adapts frame rate and picture quality to your link, lowering the "
            "frame rate first and the picture only when even the lowest frame rate "
            "does not fit; a still page gets one sharp frame. A fixed value pins the "
            "picture quality and leaves only the frame rate adaptive.",
            enum=list(LIVE_QUALITIES),
        ),
    )
    max_fps: int = field(
        default=LIVE_MAX_FPS_DEFAULT,
        metadata=_meta(
            "Max frame rate",
            "Frames per second the live view never exceeds: 15, 30 or 60. 60 is the "
            "most this machine streams. No frames are sent while the card is off "
            "screen.",
            enum=list(LIVE_MAX_FPS_CHOICES),
        ),
    )
    mask_passwords: bool = field(
        default=True,
        metadata=_meta(
            "Mask password fields",
            "Cover password boxes in the live picture. Passwords are only ever typed "
            "by you, during a takeover; they never pass through the agent and, with "
            "this on, never appear in a frame.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserLiveConfig":
        """Build from a raw ``browser.live`` dict; a non-dict builds the defaults."""
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        return cls(
            enabled=reader.read("enabled", _safe_bool),
            quality=_validated_quality(reader.get("quality")),
            max_fps=_validated_max_fps(reader.get("max_fps")),
            mask_passwords=reader.read("mask_passwords", _safe_bool),
        )


#: ``browser.recording`` byte caps, in MB. The defaults hold minutes of steady
#: change per chat; the bounds keep a hand-edited value from meaning "no cap".
RECORDING_SESSION_MB_DEFAULT = 200
RECORDING_TOTAL_MB_DEFAULT = 1024
RECORDING_MB_MIN = 1
RECORDING_MB_MAX = 100_000


@dataclass
class BrowserRecordingConfig:
    """``browser.recording``: the per-turn replay of what the agent did in the browser."""

    enabled: bool = field(
        default=True,
        metadata=_meta(
            "Record browser turns for replay",
            "Keep a replay of each turn the agent browses: masked pictures of the page "
            "and the clicks, typing and scrolls, so you can play the turn back later. "
            "Password fields are covered before a picture is kept, and typed secrets "
            "are kept as a length only. Kept only on this machine, and removed with "
            "the chat.",
        ),
    )
    max_session_mb: int = field(
        default=RECORDING_SESSION_MB_DEFAULT,
        metadata=_meta(
            "Replay space per chat (MB)",
            "The most replay one chat keeps. Past it the chat's oldest turns are removed first.",
        ),
    )
    max_total_mb: int = field(
        default=RECORDING_TOTAL_MB_DEFAULT,
        metadata=_meta(
            "Replay space in total (MB)",
            "The most replay all chats keep together. Past it the oldest turns are "
            "removed first, whichever chat they belong to.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserRecordingConfig":
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        return cls(
            enabled=reader.read("enabled", _safe_bool),
            max_session_mb=reader.read(
                "max_session_mb", _safe_int, RECORDING_MB_MIN, RECORDING_MB_MAX
            ),
            max_total_mb=reader.read("max_total_mb", _safe_int, RECORDING_MB_MIN, RECORDING_MB_MAX),
        )


#: Most ``browser.credentials.domains`` entries kept; an allowlist is a handful of
#: company domains, and the rest of a longer list is dropped with one warning.
CREDENTIAL_DOMAINS_CAP = 64
#: One ``browser.credentials.domains`` entry as the Settings write accepts it: two
#: or more lowercase DNS labels, no port, wildcard or leading dot. The seam
#: (``browser_harness.credentials``) compiles this same pattern for what a provider
#: declares, and also refuses an all-digit IP literal.
CREDENTIAL_DOMAIN_PATTERN = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}"


def coerce_credential_domains(raw: object) -> list[str]:
    """``browser.credentials.domains`` as lowercase host names; ``[]`` when unset.

    An empty list is meaningful: it lets the edition's provider supply the
    default. Anything that is not a list is unset too. Entries
    keep only what can name a host (a leading dot is dropped); the precise check,
    which refuses a single label, an IP or a wildcard, is the seam's own
    (``browser_harness.credentials.normalize_declared_domains``).
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        host = item.strip().lower().lstrip(".")
        if host and host not in out:
            out.append(host)
    if len(out) > CREDENTIAL_DOMAINS_CAP:
        _warn_once("browser.credentials.domains", f"{len(out)} entries", "the first 64", ())
        out = out[:CREDENTIAL_DOMAINS_CAP]
    return out


@dataclass
class BrowserCredentialsConfig:
    """``browser.credentials``: whether an edition's sign-in reaches Kiro's Chromium."""

    enabled: bool | None = field(
        default=None,
        metadata=_meta(
            "Use your organisation's sign-in",
            "Let Kiro's own Chromium start signed in to the sites below, using the "
            "sign-in your edition of Kiro Crew provides. Unset follows the edition's "
            "default; with no edition provider it does nothing. Your own Chrome is "
            "never changed.",
        ),
    )
    domains: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Sign-in domains",
            "The sites that sign-in may be used for, such as `example.com` (which "
            "covers its subdomains). Only a domain both listed here and served by "
            "the edition is used. Empty follows the edition's default.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserCredentialsConfig":
        if not isinstance(section, dict):
            return cls()
        enabled = section.get("enabled")
        return cls(
            enabled=enabled if isinstance(enabled, bool) else None,
            domains=coerce_credential_domains(section.get("domains")),
        )


#: Most entries kept in each of ``browser.sites.allow`` and ``browser.sites.deny``;
#: the rest of a longer list is dropped with one warning.
SITE_LIST_CAP = 256
#: One ``browser.sites`` entry: a host name (labels of letters, digits, ``-`` and
#: ``_``), optionally led by ``*.`` for "this name and every name under it", or an
#: IP literal. No scheme, port, path or other wildcard.
_SITE_ENTRY_RE = _re.compile(
    r"(?:\*\.)?[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
    r"(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*"
)


def _site_entry(item: str) -> str | None:
    """*item* as a canonical ``browser.sites`` entry, or None when it names no host.

    Scheme, userinfo, port and path are dropped and a leading ``.`` means ``*.``.
    A Unicode name goes through the apps' one host normalizer, which names no host
    for characters the two IDNA versions read differently (never another site).
    """
    import ipaddress

    from kiro_crew.apps.app_hosts import normalize_host

    text = item.strip()
    try:
        return str(ipaddress.IPv6Address(text.strip("[]")))
    except ValueError:
        pass
    try:
        host = _urlsplit(text if "://" in text else "//" + text).hostname or ""
    except ValueError:
        return None
    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            return None
    wild = host.startswith(("*.", "."))
    name = normalize_host((host[2:] if host.startswith("*.") else host.lstrip(".")).rstrip("."))
    if name is None or len(name) > 253 or not _SITE_ENTRY_RE.fullmatch(name):
        return None
    return "*." + name if wild else name


def _sites_unread(what: str) -> None:
    """Record that ``browser.sites`` held a value the loader cannot read as lists.

    The lists then load empty, which reads as no limit, so the record is what the
    navigate gate asks (``config.resolution.browser_site_lists_unknown``) before it
    opens a page. Like every degraded section it lasts the process: the operator
    fixes the file and restarts.
    """
    from kiro_crew.config.resolution import _OBSERVED_DEGRADED_SECTIONS, DEGRADED_BROWSER_SITES

    _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_BROWSER_SITES)
    if what not in _WARNED_BROWSER_VALUES:
        _WARNED_BROWSER_VALUES.add(what)
        logger.warning(
            "config: %s, so the agent's browser opens no page until the value is fixed "
            "and Kiro Crew restarts",
            what,
        )


def coerce_site_list(raw: object, key: str) -> list[str]:
    """``browser.sites.<key>`` as canonical entries; ``[]`` when unset or not a list.

    Each entry is read by :func:`_site_entry`. Any other string (``*example.com``)
    is KEPT as typed, with one warning: it never matches a page, so an allow list
    of only such entries still refuses every site instead of imposing no limit.
    Non-strings and duplicates are dropped, and past :data:`SITE_LIST_CAP` the rest
    with one warning. A value that is set but is not a list loads as ``[]`` and is
    recorded (:func:`_sites_unread`), so the navigate gate opens no page.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        _sites_unread(f"browser.sites.{key} is not a list")
        return []
    out: list[str] = []
    seen: set[str] = set()
    over = 0
    warned = False
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        entry = _site_entry(item)
        if entry is None:
            if not warned:  # one line per list, never one per bad entry
                _warn_once(f"browser.sites.{key}", item[:253], "an entry that matches no site", ())
                warned = True
            entry = item.strip().lower()[:253]
        if entry in seen:
            continue
        if len(out) >= SITE_LIST_CAP:
            over += 1
            continue
        seen.add(entry)
        out.append(entry)
    if over:
        _warn_once(f"browser.sites.{key}", f"{len(out) + over} entries", "the first 256", ())
    return out


@dataclass
class BrowserSitesConfig:
    """``browser.sites``: the sites the agent's browser may open, and those it may not."""

    allow: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed sites",
            "When not empty, the agent's browser opens only these sites: `example.com` "
            "is that host, `*.example.com` is example.com and every name under it. "
            "Checked on every page the browser lands on, after redirects and in new "
            "tabs. Empty adds no limit.",
        ),
    )
    deny: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Blocked sites",
            "Sites the agent's browser never opens, in the same form as the allowed "
            "sites. A blocked site stays blocked even when the allowed sites cover it.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserSitesConfig":
        if section is None:
            return cls()
        if not isinstance(section, dict):
            _sites_unread("browser.sites is not an object")
            return cls()
        return cls(
            allow=coerce_site_list(section.get("allow"), "allow"),
            deny=coerce_site_list(section.get("deny"), "deny"),
        )


@dataclass
class BrowserConfig:
    """``browser``: the browser the agent drives, and the live view in chat.

    Every field is hot-applied: the supervisor reads ``choice`` when it next
    launches or attaches a browser, and the screencast reads ``live`` when it
    next starts a stream.
    """

    choice: str = field(
        default=BROWSER_CHOICE_AUTO,
        metadata=_meta(
            "Browser",
            "Which browser the agent drives. `auto` picks your Chrome (with your "
            "logins, once you allow remote debugging in it), then Edge on Windows, "
            "then Kiro's own Chromium without a window on a machine with no display. "
            "`user_chrome` and `kiro_chromium` pin one of those; `headless` runs "
            "Kiro's Chromium without a window even on a desktop.",
            enum=list(BROWSER_CHOICES),
        ),
    )
    live: BrowserLiveConfig = field(
        default_factory=BrowserLiveConfig,
        metadata=_meta("Live view", "The live view of the agent's browser in chat."),
    )
    credentials: BrowserCredentialsConfig = field(
        default_factory=BrowserCredentialsConfig,
        metadata=_meta(
            "Sign-in for Kiro's Chromium",
            "Whether the edition's sign-in is put into Kiro's own Chromium, and for "
            "which domains.",
        ),
    )
    recording: BrowserRecordingConfig = field(
        default_factory=BrowserRecordingConfig,
        metadata=_meta(
            "Turn replay",
            "The per-turn replay of the agent's browsing, and how much of it is kept.",
        ),
    )
    sites: BrowserSitesConfig = field(
        default_factory=BrowserSitesConfig,
        metadata=_meta(
            "Site lists",
            "The sites the agent's browser may open and the sites it never opens. "
            "Both empty adds no limit.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserConfig":
        """Build from a raw ``browser`` dict -- the ONE parse site.

        Every value is NORMALIZED rather than validated-and-rejected, like
        :meth:`DecisionsConfig.from_raw`: a hand-edited ``config.json`` must not
        stop the gateway booting, and every key here has a value that cannot make
        things worse (``auto`` finds a browser; the ladder's top rung is what it
        would reach anyway). Accepts whatever ``json.loads`` produced, ``None`` and
        a non-dict included.
        """
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        return cls(
            choice=_validated_choice(reader.get("choice")),
            live=BrowserLiveConfig.from_raw(section.get("live")),
            credentials=BrowserCredentialsConfig.from_raw(section.get("credentials")),
            recording=BrowserRecordingConfig.from_raw(section.get("recording")),
            sites=BrowserSitesConfig.from_raw(section.get("sites")),
        )
