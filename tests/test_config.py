from pathlib import Path
import re

import pytest

from viewwall.config import ConfigError, load_config, parse_fraction


def test_parse_fraction_variants() -> None:
    assert parse_fraction("1/3", "value").numerator == 1
    assert parse_fraction("1/3", "value").denominator == 3
    # The denominator is optional, so a whole number needs no "/1".
    assert parse_fraction(0, "value") == parse_fraction("0/1", "value")
    assert parse_fraction(1, "value") == parse_fraction("1/1", "value")
    assert parse_fraction("1", "value") == parse_fraction(1, "value")


@pytest.mark.parametrize(
    "value",
    [0.5, "0.5", "0.33", "1e-2", "33%", "1/0", "abc", "-1", "-1/3", "1/2/3", "\u0663", True],
)
def test_a_coordinate_that_is_not_an_exact_fraction_is_rejected(value: object) -> None:
    # Decimals and percentages invite 0.33 or "33%" for a third of the screen,
    # which is not a third: at 1920 that leaves 19 pixels unpainted and makes
    # the columns uneven, with nothing to say so.
    with pytest.raises(ConfigError):
        parse_fraction(value, "value")


def test_load_config_expands_uri_without_exposing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "${CAMERA_RTSP}"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    config = load_config(config_path, {"CAMERA_RTSP": "rtsp://user:secret@example.invalid/feed"})
    assert config.feeds["camera"].uri.endswith("/feed")
    assert config.viewports[0].name == "viewport1"


def test_missing_environment_variable_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "${CAMERA_RTSP}"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="CAMERA_RTSP"):
        load_config(config_path, {})


def test_several_feeds_rotate_without_being_told_to(tmp_path: Path) -> None:
    # Listing more than one feed is the request to rotate; the interval is a
    # detail with a sensible default.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.one]
uri = "rtsp://example.invalid/one"
[feeds.two]
uri = "rtsp://example.invalid/two"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["one", "two"]
""",
        encoding="utf-8",
    )
    from viewwall.config import DEFAULT_ROTATE_SECONDS

    assert load_config(config_path, {}).viewports[0].rotate_seconds == DEFAULT_ROTATE_SECONDS


def test_a_single_feed_viewport_does_not_rotate(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.one]
uri = "rtsp://example.invalid/one"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["one"]
""",
        encoding="utf-8",
    )
    assert load_config(config_path, {}).viewports[0].rotate_seconds is None


def _tls_config(tmp_path: Path, extra_defaults: str = "", extra_feed: str = "") -> object:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[feed_defaults]
{extra_defaults}

[feeds.camera]
uri = "rtsps://nvr.invalid:7441/feed"
{extra_feed}

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    return load_config(config_path, {})


def test_tls_is_verified_by_default(tmp_path: Path) -> None:
    assert _tls_config(tmp_path).feeds["camera"].verify_tls is True


def test_tls_verification_can_be_disabled_per_feed(tmp_path: Path) -> None:
    config = _tls_config(tmp_path, extra_feed="verify_tls = false")
    assert config.feeds["camera"].verify_tls is False


def test_tls_verification_default_applies_to_every_feed(tmp_path: Path) -> None:
    config = _tls_config(tmp_path, extra_defaults="verify_tls = false")
    assert config.feeds["camera"].verify_tls is False


def test_a_non_boolean_verify_tls_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="verify_tls"):
        _tls_config(tmp_path, extra_feed='verify_tls = "maybe"')


def test_there_is_no_partial_verification_mode() -> None:
    from viewwall.config import TLS_NO_VERIFY_FLAGS, TLS_VERIFY_FLAGS

    # A partial mask silently behaves as full validation on this GStreamer, so
    # the setting is a boolean. Measured: only 0 connects.
    assert TLS_NO_VERIFY_FLAGS == 0
    assert TLS_VERIFY_FLAGS != 0


def test_query_parameters_reach_rtspsrc_verbatim(tmp_path: Path) -> None:
    # Viewwall never rewrites a URI; server-specific parameters pass through.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "rtsps://nvr.invalid:7441/feed?profile=high"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    assert config.feeds["camera"].uri.endswith("?profile=high")


def _metrics_config(tmp_path: Path, table: str = "") -> object:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
{table}

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    return load_config(config_path, {})


def test_metrics_are_on_by_default(tmp_path: Path) -> None:
    # Instrumentation you must enable before reproducing a problem is
    # instrumentation you do not have when it matters.
    metrics = _metrics_config(tmp_path).metrics
    assert metrics.interval_seconds == 60.0
    assert metrics.enabled is True


def test_metrics_interval_is_configurable(tmp_path: Path) -> None:
    config = _metrics_config(tmp_path, "[metrics]\ninterval_seconds = 10")
    assert config.metrics.interval_seconds == 10.0


def test_zero_interval_disables_metrics(tmp_path: Path) -> None:
    config = _metrics_config(tmp_path, "[metrics]\ninterval_seconds = 0")
    assert config.metrics.enabled is False


def test_negative_metrics_interval_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="interval_seconds"):
        _metrics_config(tmp_path, "[metrics]\ninterval_seconds = -1")


def test_non_numeric_metrics_interval_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="interval_seconds"):
        _metrics_config(tmp_path, '[metrics]\ninterval_seconds = "often"')


def _viewport_config(tmp_path: Path, viewport_extra: str = "") -> object:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[feeds.one]
uri = "rtsp://example.invalid/one"
[feeds.two]
uri = "rtsp://example.invalid/two"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["one", "two"]
{viewport_extra}
""",
        encoding="utf-8",
    )
    return load_config(config_path, {})


def test_rotate_seconds_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="rotate_seconds"):
        _viewport_config(tmp_path, "rotate_seconds = 0")


def test_a_rotating_viewport_starts_on_its_first_feed(tmp_path: Path) -> None:
    assert _viewport_config(tmp_path, "rotate_seconds = 5").viewports[0].feeds[0] == "one"


def test_rotate_seconds_on_a_single_feed_viewport_is_harmless(tmp_path: Path) -> None:
    # Accepted and inert, rather than an error: there is nothing to rotate.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.one]
uri = "rtsp://example.invalid/one"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["one"]
rotate_seconds = 5
""",
        encoding="utf-8",
    )
    assert load_config(config_path, {}).viewports[0].feeds == ("one",)


@pytest.mark.parametrize(
    ("table", "setting"),
    (
        ("display", "poll_interval_seconds = true"),
        ("display", "connector_id = true"),
        ("layout", "gap_px = true"),
        ("layout", "outer_margin_px = true"),
        ("feed_defaults", "latency_ms = true"),
    ),
)
def test_a_boolean_is_not_accepted_as_a_number(
    tmp_path: Path, table: str, setting: str
) -> None:
    # bool subclasses int, so isinstance(True, int) passes: "gap_px = true"
    # would silently have meant one pixel.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[{table}]
{setting}

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path, {})


def test_a_non_numeric_interval_is_a_config_error(tmp_path: Path) -> None:
    # Previously float("abc") escaped as a bare ValueError traceback.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
poll_interval_seconds = "often"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="poll_interval_seconds"):
        load_config(config_path, {})


def test_a_non_string_device_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
device = 42

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="device"):
        load_config(config_path, {})


def test_the_default_transport_is_tcp(tmp_path: Path) -> None:
    # Not "auto", which is rtspsrc's default: it resolves to UDP, and on a Pi 3
    # with nine feeds UDP dies within seconds when GStreamer cannot allocate a
    # timer thread. Measured over 90s runs: tcp survived, auto died after 6s.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    assert load_config(config_path, {}).feeds["camera"].transport == "tcp"


def test_auto_transport_is_offered(tmp_path: Path) -> None:
    # rtspsrc's own negotiation remains reachable for anyone whose hardware
    # can take it; it is simply not the default.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feed_defaults]
transport = "auto"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    assert load_config(config_path, {}).feeds["camera"].transport == "auto"


_MINIMAL_VIEWPORTS = """
[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
"""


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[displays.tv]\nconector_id = 35\n',
            "unknown key(s) in displays.tv: conector_id",
        ),
        ('[drm]\ndevices = "/dev/dri/card0"\n', "unknown key(s) in drm: devices"),
        (
            '[display_defaults]\ngap = 1\n',
            "unknown key(s) in display_defaults: gap",
        ),
        ('[metrics]\ninterval = 60\n', "unknown key(s) in metrics: interval"),
        ('[feed_defaults]\ntls = true\n', "unknown key(s) in feed_defaults: tls"),
        # [layout] became per-display spacing; [display] became [displays.<name>].
        ('[layout]\ngap_px = 1\n', "unknown top-level key(s): layout"),
        ('[display]\ngap_px = 1\n', "unknown top-level key(s): display"),
        ('[system]\ndevice = "/dev/dri/card0"\n', "unknown top-level key(s): system"),
        # stream_defaults became feed_defaults: the domain object is a feed.
        (
            '[stream_defaults]\nlatency_ms = 200\n',
            "unknown top-level key(s): stream_defaults",
        ),
    ],
)
def test_unknown_table_keys_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    # A silently ignored key is a typo that costs an afternoon: "conector_id"
    # would leave the wall on the wrong display with nothing in the log.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        body + '\n[feeds.camera]\nuri = "rtsp://nvr.invalid/feed"\n' + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(config_path, {})


def test_unknown_feed_and_viewport_keys_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
protocol = "tcp"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    # "protocol" was this option's name before it became "transport", so a
    # stale config says so rather than quietly running the default.
    with pytest.raises(ConfigError, match="unknown key\\(s\\) in feeds.camera: protocol"):
        load_config(config_path, {})

    config_path.write_text(
        """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
initial_feed = "camera"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown key\\(s\\) in viewports\\[0\\]: initial_feed"):
        load_config(config_path, {})


def test_unknown_top_level_table_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[nonsense]
value = 1

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown top-level key\\(s\\): nonsense"):
        load_config(config_path, {})


def test_display_overrides_display_defaults(tmp_path: Path) -> None:
    # The same relationship a feed has to [feed_defaults]: the defaults
    # table supplies a value, the specific one wins where it names it.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[display_defaults]
gap_px = 4
outer_margin_px = 10

[displays.tv]
connector_id = 35
gap_px = 1

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    display = config.displays[0]
    assert display.gap_px == 1
    assert display.outer_margin_px == 10
    # resolve_layout takes the spacing half, whichever table supplied it.
    layout = config.layout_for(display)
    assert layout.gap_px == 1
    assert layout.outer_margin_px == 10


def test_drm_holds_card_wide_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
device = "/dev/dri/card1"
poll_interval_seconds = 30

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    assert config.drm.device == "/dev/dri/card1"
    assert config.drm.poll_interval_seconds == 30


def test_one_display_needs_no_connector_id(tmp_path: Path) -> None:
    """A single table has no ambiguity to resolve.

    It describes the one display, exactly as the no-table case does, so
    naming it should not require looking an id up in kmsprint.
    """
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.main]
mode = "1280x720"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    assert config.displays[0].connector_id is None
    assert config.displays[0].mode == (1280, 720)


def test_several_displays_still_need_connector_ids(tmp_path: Path) -> None:
    # With two, which screen shows what would depend on probe order.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.main]
[displays.hall]

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="more than one display"):
        load_config(config_path, {})


@pytest.mark.parametrize(
    ("written", "expected"),
    [("1280x720", (1280, 720)), ("800x600", (800, 600)), ("  640x480  ", (640, 480))],
)
def test_mode_is_parsed(tmp_path: Path, written: str, expected) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[displays.main]
connector_id = 35
mode = "{written}"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    assert load_config(config_path, {}).displays[0].mode == expected


def test_mode_defaults_to_keeping_the_current_one(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    assert load_config(config_path, {}).displays[0].mode is None


@pytest.mark.parametrize("written", ["1280", "1280X720", "abc", "0x600", "1280x"])
def test_mode_rejects_anything_but_width_by_height(
    tmp_path: Path, written: str
) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[displays.main]
connector_id = 35
mode = "{written}"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="mode"):
        load_config(config_path, {})


def test_background_defaults_to_black(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    assert load_config(config_path, {}).drm.background == "#000000"


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("#123456", "#123456"),
        # Three digits expand, so the runtime only ever handles one shape.
        ("#abc", "#AABBCC"),
        ("#AbCdEf", "#ABCDEF"),
        ("none", None),
        # Disabling is a plain word, and the case it is written in is the
        # user's business rather than something to reject.
        ("NONE", None),
    ],
)
def test_background_accepts_colours_and_none(
    tmp_path: Path, written: str, expected: str | None
) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[drm]
background = "{written}"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    assert load_config(config_path, {}).drm.background == expected


@pytest.mark.parametrize(
    "written",
    ["black", "#12345", "#gggggg", "", "000000"],
)
def test_background_rejects_anything_else(tmp_path: Path, written: str) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"""
[drm]
background = "{written}"

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="drm.background"):
        load_config(config_path, {})


def test_background_rejects_a_non_string(tmp_path: Path) -> None:
    # A bare colour looks like a number to TOML, and "must be a string" says
    # more about the fix than a failed hex match would.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
background = 0

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="drm.background must be a string"):
        load_config(config_path, {})


def test_spacing_defaults_to_zero_without_either_table(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        '[feeds.camera]\nuri = "rtsp://nvr.invalid/feed"\n' + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    display = config.displays[0]
    assert config.layout_for(display).gap_px == 0
    assert config.layout_for(display).outer_margin_px == 0
    assert config.drm.device == "/dev/dri/card0"
    # A configuration that names no display still has one, discovered.
    assert display.name == "main"
    assert display.connector_id is None


@pytest.mark.parametrize(
    ("table", "key", "value", "message"),
    [
        ("[display_defaults]", "gap_px", "-1", "display_defaults.gap_px"),
        ("[displays.tv]", "gap_px", "true", "displays.tv.gap_px"),
        ("[displays.tv]", "outer_margin_px", "-2", "displays.tv.outer_margin_px"),
    ],
)
def test_spacing_rejects_negative_and_bool(
    tmp_path: Path, table: str, key: str, value: str, message: str
) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        f"{table}\n{key} = {value}\n"
        '\n[feeds.camera]\nuri = "rtsp://nvr.invalid/feed"\n' + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(config_path, {})


def _two_displays(extra_viewport: str = "") -> str:
    return (
        """
[displays.left]
connector_id = 35

[displays.right]
connector_id = 36
gap_px = 2

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "left"
"""
        + extra_viewport
    )


def test_several_displays_partition_their_viewports(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        _two_displays(
            """
[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "right"
"""
        ),
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    left, right = config.displays
    assert [viewport.name for viewport in config.viewports_for(left)] == ["viewport1"]
    assert [viewport.name for viewport in config.viewports_for(right)] == ["viewport2"]
    # Each display carries its own spacing, so a seam on one is not a seam on
    # the other.
    assert config.layout_for(left).gap_px == 0
    assert config.layout_for(right).gap_px == 2


def test_a_viewport_must_name_its_display_when_several_exist(tmp_path: Path) -> None:
    # Defaulting would make which screen shows what depend on table order.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        _two_displays(
            """
[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
"""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must name a display"):
        load_config(config_path, {})


def test_a_viewport_naming_an_unknown_display_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        _two_displays(
            """
[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "middle"
"""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown display 'middle'"):
        load_config(config_path, {})


def test_several_displays_each_need_a_connector_id(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.left]

[displays.right]
connector_id = 36

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "right"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="connector_id is required"):
        load_config(config_path, {})


def test_a_lone_display_needs_no_table_at_all(tmp_path: Path) -> None:
    # Spacing alone does not require naming a display: [display_defaults]
    # applies to the discovered one like any other.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        "[display_defaults]\ngap_px = 1\n"
        '\n[feeds.camera]\nuri = "rtsp://nvr.invalid/feed"\n' + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    assert config.displays[0].connector_id is None
    assert config.displays[0].gap_px == 1
    # And a viewport need not name it.
    assert config.viewports[0].display == "main"


def test_two_displays_may_not_share_a_connector(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.left]
connector_id = 35

[displays.right]
connector_id = 35

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "left"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="both use connector_id 35"):
        load_config(config_path, {})


def test_duplicate_display_names_are_rejected(tmp_path: Path) -> None:
    # Naming a display in the table header rather than in a key means TOML
    # itself catches the duplicate, before any of this code runs.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.tv]
connector_id = 35

[displays.tv]
connector_id = 36

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"
"""
        + _MINIMAL_VIEWPORTS,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(config_path, {})


def test_viewports_are_identified_by_their_position(tmp_path: Path) -> None:
    # A viewport has no name to configure. Nothing refers to one, and the label
    # worth having -- the feed it shows -- is already reported beside it.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.porch]
uri = "rtsp://nvr.invalid/a"

[feeds.yard]
uri = "rtsp://nvr.invalid/b"

[[viewports]]
x = "0"
y = "0"
width = "1/2"
height = "1"
feeds = ["porch"]

[[viewports]]
x = "1/2"
y = "0"
width = "1/2"
height = "1"
feeds = ["porch", "yard"]
""",
        encoding="utf-8",
    )
    viewports = load_config(config_path, {}).viewports
    assert [viewport.index for viewport in viewports] == [1, 2]
    assert [viewport.name for viewport in viewports] == ["viewport1", "viewport2"]


def test_a_viewport_may_not_be_named(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.porch]
uri = "rtsp://nvr.invalid/a"

[[viewports]]
name = "left"
x = 0
y = 0
width = 1
height = 1
feeds = ["porch"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=re.escape("unknown key(s) in viewports[0]: name")):
        load_config(config_path, {})


def test_one_feed_may_fill_two_viewports(tmp_path: Path) -> None:
    # Showing a camera in two places is a layout, not a mistake, and needs no
    # disambiguation now that viewports are numbered.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.porch]
uri = "rtsp://nvr.invalid/a"

[[viewports]]
x = "0"
y = "0"
width = "1/2"
height = "1"
feeds = ["porch"]

[[viewports]]
x = "1/2"
y = "0"
width = "1/2"
height = "1"
feeds = ["porch"]
""",
        encoding="utf-8",
    )
    viewports = load_config(config_path, {}).viewports
    assert [viewport.name for viewport in viewports] == ["viewport1", "viewport2"]
    assert all(viewport.feeds == ("porch",) for viewport in viewports)


def test_a_lone_named_display_takes_viewports_that_name_none(tmp_path: Path) -> None:
    # "the only display there is" rather than "the unnamed display": naming a
    # display does not force every viewport to repeat that name, and the viewport
    # inherits its spacing.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.hall]
connector_id = 36
gap_px = 7
outer_margin_px = 3

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
""",
        encoding="utf-8",
    )
    config = load_config(config_path, {})
    assert config.viewports[0].display == "hall"
    display = config.displays[0]
    # The pinned connector wins; there is no fall back to the first output.
    assert display.connector_id == 36
    assert config.layout_for(display).gap_px == 7
    assert config.layout_for(display).outer_margin_px == 3
    assert [viewport.name for viewport in config.viewports_for(display)] == ["viewport1"]


def test_a_feed_named_by_an_empty_table_key_is_rejected(tmp_path: Path) -> None:
    # The key is the feed's identity: it is what a viewport refers to and what
    # the GStreamer element names are built from, so an empty one would yield
    # elements called "tee_" and a metrics line naming no feed at all.
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[feeds.""]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = [""]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="feed name must not be empty"):
        load_config(config_path)


def test_a_display_named_by_an_empty_table_key_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[displays.""]
connector_id = 32

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = ""
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="display name must not be empty"):
        load_config(config_path)
