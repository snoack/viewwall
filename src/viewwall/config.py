from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass
from fractions import Fraction
import os
from pathlib import Path
import re
import tomllib
from typing import Any


# One line per feed per minute is a few thousand journal entries a day, which
# is cheap next to needing the numbers and not having them. 0 disables.
DEFAULT_METRICS_INTERVAL_SECONDS = 60.0
# Long enough to take in a camera, short enough that a rotating viewport is not
# effectively static.
DEFAULT_ROTATE_SECONDS = 8.0

# rtspsrc's "protocols" flags. "auto" is rtspsrc's own default: offer every
# lower transport and let the server choose, which it does in the order UDP,
# UDP multicast, TCP.
#
# Viewwall defaults to tcp because that is what has been shown to work at nine
# feeds. On the one setup measured -- a Pi 3 against a UniFi Protect NVR --
# anything resolving to UDP unicast died within seconds, GStreamer reporting
# that it could not create a thread before GLib aborted the process. Which of
# the two ends is the constraint was not isolated; see the Transport section
# of DESIGN.md for what the measurement does and does not establish.
TRANSPORTS: dict[str, str] = {
    "auto": "tcp+udp-mcast+udp",
    "tcp": "tcp",
    "udp": "udp",
    "udp-mcast": "udp-mcast",
}

# rtspsrc's tls-validation-flags: every condition, or none. GLib deprecated
# per-condition flags and this GStreamer treats any non-zero value as full
# validation, so there is no middle setting to offer. Measured against a
# Protect NVR: flags 122/126/127 all fail and only 0 connects.
TLS_VERIFY_FLAGS = 0x7F
TLS_NO_VERIFY_FLAGS = 0


class ConfigError(ValueError):
    """Raised when the configuration is incomplete or inconsistent."""


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Anything a fraction cannot contain. "." and "e" are the ones that matter:
# Fraction reads "0.33" and "1e-2" happily, and both are the imprecise
# spelling this refuses. Also catches signs and non-ASCII digits.
_NOT_FRACTION_RE = re.compile(r"[^0-9/]")
_HEX_COLOUR_RE = re.compile(r"^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


def parse_fraction(value: object, field: str) -> Fraction:
    """Read a screen coordinate as an exact fraction, "N/D" or a whole number.

    The parts are checked before Fraction sees them rather than after. Fraction
    accepts strings, but it also accepts "0.33" and "1e-2" and turns them into
    ordinary rationals -- once it has parsed, 33/100 is indistinguishable from
    a deliberate "33/100" and the decimal spelling can no longer be refused.

    That spelling is the whole reason to be strict. A third of the screen
    invites 0.33 or "33%", which is not a third: at 1920 it leaves a 19-pixel
    strip unpainted down one edge and makes the columns 634/633/634 instead of
    640 each, with nothing to say so. A fraction cannot express the mistake.
    """
    maybe_fraction = (
        isinstance(value, int)
        and not isinstance(value, bool)
        or isinstance(value, str)
        and not _NOT_FRACTION_RE.search(value)
    )
    if maybe_fraction:
        try:
            fraction = Fraction(value)
            if fraction >= 0:
                return fraction
        except (ValueError, ZeroDivisionError):
            pass
    raise ConfigError(
        f"{field} must be a whole number or a valid fraction such as "
        f"\"1/3\", not {value!r}"
    )


def _expand_environment(value: str, environ: Mapping[str, str]) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ:
            missing.append(name)
            return ""
        return environ[name]

    expanded = _ENV_RE.sub(replace, value)
    if missing:
        raise ConfigError(f"missing environment variable(s): {', '.join(sorted(set(missing)))}")
    return expanded


@dataclass(frozen=True)
class RectSpec:
    x: Fraction
    y: Fraction
    width: Fraction
    height: Fraction

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], field: str) -> "RectSpec":
        """Read x, y, width and height, each a fraction of the screen.

        Named rather than a four-element list: the order of such a list is a
        convention to be looked up, and [x, y, width, height] is
        indistinguishable at a glance from [left, top, right, bottom].
        """
        missing = [key for key in ("x", "y", "width", "height") if key not in raw]
        if missing:
            raise ConfigError(f"{field} is missing {', '.join(missing)}")
        rect = cls(
            x=parse_fraction(raw["x"], f"{field}.x"),
            y=parse_fraction(raw["y"], f"{field}.y"),
            width=parse_fraction(raw["width"], f"{field}.width"),
            height=parse_fraction(raw["height"], f"{field}.height"),
        )
        # Negatives cannot reach here -- parse_fraction takes no sign -- so
        # zero extent is all that is left to reject.
        if rect.width == 0 or rect.height == 0:
            raise ConfigError(f"{field} must have a positive width and height")
        if rect.x + rect.width > 1 or rect.y + rect.height > 1:
            raise ConfigError(f"{field} extends outside the normalized display area")
        return rect


@dataclass(frozen=True)
class DrmConfig:
    """How to reach the DRM card, which is one card however many outputs.

    "device" names the card, not a connector: displays are connectors
    enumerated within it, and it is opened once into a file descriptor every
    kmssink shares. "poll_interval_seconds" paces a single timer whose probe
    already reports every connector, since "kmsprint -l" dumps the whole card.
    """

    device: str = "/dev/dri/card0"
    poll_interval_seconds: float = 2.0
    # What to paint underneath the viewports. Nothing draws there otherwise,
    # so the framebuffer console shows through wherever no viewport covers:
    # the gaps, the outer margin, and any viewport whose plane is disabled
    # because every feed behind it is down. None leaves the console alone,
    # which "none" in the file selects.
    background: str | None = "#000000"


DEFAULT_DISPLAY_NAME = "main"


@dataclass(frozen=True)
class DisplayConfig:
    """One output: which connector, at what mode, with what spacing.

    Each display is its own 0..1 canvas. A viewport cannot span two of them --
    a KMS plane belongs to exactly one CRTC -- so there is no shared
    coordinate space to place viewports in, and "outer_margin_px" means the edge
    of this screen rather than the edge of some union of screens.
    """

    name: str = DEFAULT_DISPLAY_NAME
    connector_id: int | None = None
    width: int | None = None
    height: int | None = None
    gap_px: int = 0
    outer_margin_px: int = 0


@dataclass(frozen=True)
class MetricsConfig:
    """Periodic reporting of rendered framerate and queue occupancy.

    On by default: instrumentation that has to be switched on before a problem
    can be reproduced is instrumentation you do not have when it matters.
    """

    interval_seconds: float = DEFAULT_METRICS_INTERVAL_SECONDS

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0


@dataclass(frozen=True)
class LayoutConfig:
    """The spacing half of a display, as resolve_layout wants it."""

    gap_px: int = 0
    outer_margin_px: int = 0


@dataclass(frozen=True)
class FeedDefaults:
    latency_ms: int = 150
    transport: str = "tcp"
    verify_tls: bool = True


@dataclass(frozen=True)
class FeedConfig:
    name: str
    uri: str
    latency_ms: int
    transport: str
    verify_tls: bool


@dataclass(frozen=True)
class ViewportConfig:
    """One viewport. Identified by its position in the file, not by a name.

    Nothing refers to a viewport, so a name was only ever a label for logs and
    metrics -- and the obvious label, the feed it shows, is already reported
    beside it. The index says which [[viewports]] entry is meant without
    duplicating anything.
    """

    index: int
    rect: RectSpec
    feeds: tuple[str, ...]
    rotate_seconds: float | None = None
    display: str = DEFAULT_DISPLAY_NAME

    @property
    def name(self) -> str:
        """A stable identifier for dict keys and GStreamer element names."""
        return f"viewport{self.index}"


@dataclass(frozen=True)
class AppConfig:
    drm: DrmConfig
    displays: tuple[DisplayConfig, ...]
    metrics: MetricsConfig
    feed_defaults: FeedDefaults
    feeds: dict[str, FeedConfig]
    viewports: tuple[ViewportConfig, ...]

    @property
    def display(self) -> DisplayConfig:
        """The only configured display, which may be the discovered one.

        Most of the wall is single-display; this keeps those callers honest
        by failing loudly rather than silently picking the first of several.
        """
        if len(self.displays) != 1:
            raise ConfigError(
                "this configuration drives several displays; use displays_by_name"
            )
        return self.displays[0]

    @property
    def displays_by_name(self) -> dict[str, DisplayConfig]:
        return {display.name: display for display in self.displays}

    def layout_for(self, display: DisplayConfig) -> LayoutConfig:
        """Spacing, as resolve_layout wants it.

        Kept separate from DisplayConfig so that layout resolution takes only
        what it uses, and so each display supplies its own.
        """
        return LayoutConfig(
            gap_px=display.gap_px,
            outer_margin_px=display.outer_margin_px,
        )

    def viewports_for(self, display: DisplayConfig) -> tuple[ViewportConfig, ...]:
        return tuple(viewport for viewport in self.viewports if viewport.display == display.name)


def _positive_number(value: object, field: str) -> float:
    """A TOML number, rejecting bool.

    bool subclasses int, so isinstance(True, int) is true and an accidental
    "poll_interval_seconds = true" would otherwise mean one second.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    if value <= 0:
        raise ConfigError(f"{field} must be positive")
    return float(value)


def _background(value: object) -> str | None:
    """Normalise drm.background into a colour, or None to leave the console.

    The three-digit form is expanded here so the runtime only ever sees six,
    and the result is uppercased so a log line quoting it reads the same
    however the file was written.
    """
    if not isinstance(value, str):
        raise ConfigError("drm.background must be a string")
    text = value.strip()
    if text.lower() == "none":
        return None
    match = _HEX_COLOUR_RE.match(text)
    if match is None:
        raise ConfigError(
            'drm.background must be "none" or a hex colour such as "#000000", '
            f"not {value!r}"
        )
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    return f"#{digits.upper()}"


def _require_table_name(name: str, field: str) -> None:
    """Reject a table key that cannot name anything.

    The key is the identity: a feed is referred to by it, and it is built into
    the GStreamer element names, so an empty one yields elements called "tee_"
    and a viewport reporting a blank feed. TOML permits it, which is why it is
    checked here.
    """
    if not name:
        raise ConfigError(f"{field} name must not be empty")


def _mapping(value: object, field: str, allowed: Container[str] | None = None) -> dict[str, Any]:
    """Read a TOML table, rejecting keys that are not understood.

    A silently ignored key is a typo that costs an afternoon: "conector_id"
    would leave the wall on the wrong display with nothing in the log. It also
    keeps the schema honest, since a key that means nothing today cannot come
    to mean something later in a configuration that already sets it.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a table")
    if allowed is not None:
        unknown = sorted(key for key in value if key not in allowed)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in {field}: {', '.join(unknown)}"
            )
    return value


_DRM_KEYS = frozenset({"device", "poll_interval_seconds", "background"})
# Settings that describe one output. [display_defaults] supplies them to every
# display that does not say otherwise, including the discovered one.
_DISPLAY_DEFAULT_KEYS = frozenset({"gap_px", "outer_margin_px"})
_DISPLAY_KEYS = (
    frozenset({"connector_id", "width", "height"}) | _DISPLAY_DEFAULT_KEYS
)
_METRICS_KEYS = frozenset({"interval_seconds"})
_FEED_DEFAULT_KEYS = frozenset({"latency_ms", "transport", "verify_tls"})
_FEED_KEYS = frozenset({"uri"}) | _FEED_DEFAULT_KEYS
_VIEWPORT_KEYS = frozenset(
    {"x", "y", "width", "height", "feeds", "rotate_seconds", "display"}
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "drm",
        "displays",
        "display_defaults",
        "metrics",
        "feed_defaults",
        "feeds",
        "viewports",
    }
)


def load_config(path: str | Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    env = os.environ if environ is None else environ
    unknown_top = sorted(key for key in raw if key not in _TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown_top)}")
    drm_raw = _mapping(raw.get("drm"), "drm", _DRM_KEYS)
    display_defaults_raw = _mapping(
        raw.get("display_defaults"), "display_defaults", _DISPLAY_DEFAULT_KEYS
    )
    metrics_raw = _mapping(raw.get("metrics"), "metrics", _METRICS_KEYS)
    defaults_raw = _mapping(raw.get("feed_defaults"), "feed_defaults", _FEED_DEFAULT_KEYS)

    poll_interval = _positive_number(
        drm_raw.get("poll_interval_seconds", 2.0),
        "drm.poll_interval_seconds",
    )

    device = drm_raw.get("device", "/dev/dri/card0")
    if not isinstance(device, str) or not device:
        raise ConfigError("drm.device must be a path")

    drm = DrmConfig(
        device=device,
        poll_interval_seconds=poll_interval,
        background=_background(drm_raw.get("background", "#000000")),
    )

    def _display(display_raw: dict[str, Any], field: str, name: str) -> DisplayConfig:
        width = display_raw.get("width")
        height = display_raw.get("height")
        if (width is None) != (height is None):
            raise ConfigError(f"{field}.width and {field}.height must be set together")
        if width is not None and (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ConfigError(f"{field} dimensions must be positive integers")

        connector_id = display_raw.get("connector_id")
        if connector_id is not None and (
            isinstance(connector_id, bool)
            or not isinstance(connector_id, int)
            or connector_id <= 0
        ):
            raise ConfigError(f"{field}.connector_id must be a positive integer")

        def spacing(key: str) -> int:
            # A display overrides [display_defaults] the way a feed overrides
            # [feed_defaults].
            default = display_defaults_raw.get(key, 0)
            if isinstance(default, bool) or not isinstance(default, int) or default < 0:
                raise ConfigError(
                    f"display_defaults.{key} must be a non-negative integer"
                )
            value = display_raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{field}.{key} must be a non-negative integer")
            return value

        return DisplayConfig(
            name=name,
            connector_id=connector_id,
            width=width,
            height=height,
            gap_px=spacing("gap_px"),
            outer_margin_px=spacing("outer_margin_px"),
        )

    displays_raw = _mapping(raw.get("displays"), "displays")
    if not displays_raw:
        # The common case is one screen with nothing to say about it: no name
        # to invent and no connector to look up. Spacing alone belongs in
        # [display_defaults], which applies to it like any other display.
        displays = (_display({}, "display_defaults", DEFAULT_DISPLAY_NAME),)
    else:
        parsed: list[DisplayConfig] = []
        seen_connectors: dict[int, str] = {}
        for name, value in displays_raw.items():
            _require_table_name(name, "display")
            field = f"displays.{name}"
            display_raw = _mapping(value, field, _DISPLAY_KEYS)
            display = _display(display_raw, field, name)
            if display.connector_id is None:
                # Naming a display at all means saying which one, otherwise
                # which screen shows what would depend on the order the probe
                # happened to list connectors in.
                raise ConfigError(f"{field}.connector_id is required")
            if display.connector_id in seen_connectors:
                raise ConfigError(
                    f"displays {seen_connectors[display.connector_id]} and "
                    f"{name} both use connector_id {display.connector_id}"
                )
            seen_connectors[display.connector_id] = name
            parsed.append(display)
        displays = tuple(parsed)

    metrics_interval = metrics_raw.get(
        "interval_seconds", DEFAULT_METRICS_INTERVAL_SECONDS
    )
    if not isinstance(metrics_interval, (int, float)) or isinstance(metrics_interval, bool):
        raise ConfigError("metrics.interval_seconds must be a number")
    if metrics_interval < 0:
        raise ConfigError("metrics.interval_seconds must not be negative")
    metrics = MetricsConfig(interval_seconds=float(metrics_interval))

    latency_ms = defaults_raw.get("latency_ms", 150)
    transport = str(defaults_raw.get("transport", "tcp")).lower()
    verify_tls = defaults_raw.get("verify_tls", True)
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
        raise ConfigError("feed_defaults.latency_ms must be a non-negative integer")
    if transport not in TRANSPORTS:
        raise ConfigError(
            "feed_defaults.transport must be " + " or ".join(sorted(TRANSPORTS))
        )
    if not isinstance(verify_tls, bool):
        raise ConfigError("feed_defaults.verify_tls must be true or false")
    defaults = FeedDefaults(
        latency_ms=latency_ms,
        transport=transport,
        verify_tls=verify_tls,
    )

    feeds_raw = _mapping(raw.get("feeds"), "feeds")
    if not feeds_raw:
        raise ConfigError("at least one feed must be configured")
    feeds: dict[str, FeedConfig] = {}
    for name, value in feeds_raw.items():
        _require_table_name(name, "feed")
        feed_raw = _mapping(value, f"feeds.{name}", _FEED_KEYS)
        uri_value = feed_raw.get("uri")
        if not isinstance(uri_value, str) or not uri_value:
            raise ConfigError(f"feeds.{name}.uri is required")
        uri = _expand_environment(uri_value, env)
        if not uri.lower().startswith(("rtsp://", "rtsps://")):
            raise ConfigError(f"feeds.{name}.uri must be an RTSP URI")
        feed_latency = feed_raw.get("latency_ms", defaults.latency_ms)
        feed_transport = str(feed_raw.get("transport", defaults.transport)).lower()
        feed_verify_tls = feed_raw.get("verify_tls", defaults.verify_tls)
        if isinstance(feed_latency, bool) or not isinstance(feed_latency, int) or feed_latency < 0:
            raise ConfigError(f"feeds.{name}.latency_ms must be a non-negative integer")
        if feed_transport not in TRANSPORTS:
            raise ConfigError(
                f"feeds.{name}.transport must be " + " or ".join(sorted(TRANSPORTS))
            )
        if not isinstance(feed_verify_tls, bool):
            raise ConfigError(f"feeds.{name}.verify_tls must be true or false")
        feeds[name] = FeedConfig(
            name=name,
            uri=uri,
            latency_ms=feed_latency,
            transport=feed_transport,
            verify_tls=feed_verify_tls,
        )

    viewports_raw = raw.get("viewports")
    if not isinstance(viewports_raw, list) or not viewports_raw:
        raise ConfigError("at least one [[viewports]] entry is required")
    display_names = {display.name for display in displays}
    viewports: list[ViewportConfig] = []
    for index, value in enumerate(viewports_raw):
        viewport_raw = _mapping(value, f"viewports[{index}]", _VIEWPORT_KEYS)
        rect = RectSpec.from_mapping(viewport_raw, f"viewports[{index}]")
        viewport_feeds_raw = viewport_raw.get("feeds")
        if not isinstance(viewport_feeds_raw, list) or not viewport_feeds_raw or any(not isinstance(item, str) for item in viewport_feeds_raw):
            raise ConfigError(f"viewports[{index}].feeds must be a non-empty list")
        unknown = [item for item in viewport_feeds_raw if item not in feeds]
        if unknown:
            raise ConfigError(
                f"viewports[{index}] references unknown feed(s): {', '.join(unknown)}"
            )
        if len(set(viewport_feeds_raw)) != len(viewport_feeds_raw):
            raise ConfigError(f"viewports[{index}] contains duplicate feeds")

        rotate = viewport_raw.get("rotate_seconds")
        if rotate is not None:
            rotate = _positive_number(rotate, f"viewports[{index}].rotate_seconds")
        elif len(viewport_feeds_raw) > 1:
            # Listing several feeds already says the viewport should rotate; the
            # interval is a detail, so it does not need stating.
            rotate = DEFAULT_ROTATE_SECONDS
        viewport_display = viewport_raw.get("display")
        if viewport_display is None:
            if len(displays) == 1:
                # One display: naming it on every viewport would be noise.
                viewport_display = displays[0].name
            else:
                raise ConfigError(
                    f"viewports[{index}] must name a display with \"display\"; "
                    f"configured display(s): {', '.join(sorted(display_names))}"
                )
        if not isinstance(viewport_display, str) or not viewport_display:
            raise ConfigError(f"viewports[{index}].display must be a non-empty string")
        if viewport_display not in display_names:
            known = ", ".join(sorted(display_names))
            raise ConfigError(
                f"viewports[{index}] names unknown display {viewport_display!r}; "
                f"configured display(s): {known}"
            )
        viewports.append(
            ViewportConfig(
                index=index + 1,
                rect=rect,
                feeds=tuple(viewport_feeds_raw),
                rotate_seconds=rotate,
                display=viewport_display,
            )
        )

    return AppConfig(
        drm=drm,
        displays=displays,
        metrics=metrics,
        feed_defaults=defaults,
        feeds=feeds,
        viewports=tuple(viewports),
    )
