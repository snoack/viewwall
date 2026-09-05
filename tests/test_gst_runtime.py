import socket
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from viewwall.gst_runtime import RuntimeDependencyError, WallRuntime
from viewwall.layout import PixelRect, SourceCrop


class _FakeElementFactory:
    available: set[str] = set()

    @classmethod
    def find(cls, name: str) -> object | None:
        return object() if name in cls.available else None


class _FakeGst:
    ElementFactory = _FakeElementFactory


def _runtime_with_factories(*factories: str) -> WallRuntime:
    runtime = object.__new__(WallRuntime)
    runtime.Gst = _FakeGst
    _FakeElementFactory.available = set(factories)
    return runtime


def test_rtp_codec_detection() -> None:
    assert WallRuntime._rtp_codec("H264") == "h264"
    assert WallRuntime._rtp_codec("h.264") == "h264"
    assert WallRuntime._rtp_codec("H265") == "h265"
    assert WallRuntime._rtp_codec("HEVC") == "h265"
    assert WallRuntime._rtp_codec("VP9") is None


def test_h264_falls_back_to_software() -> None:
    runtime = _runtime_with_factories("avdec_h264")
    assert runtime._decoder_factory("h264") == ("avdec_h264", False)


def test_h265_never_falls_back_to_software() -> None:
    runtime = _runtime_with_factories("avdec_h265")
    with pytest.raises(RuntimeDependencyError, match="v4l2slh265dec"):
        runtime._decoder_factory("h265")


@pytest.mark.parametrize(
    ("failures", "delay"),
    ((0, 1), (1, 1), (2, 2), (3, 5), (4, 10), (5, 30), (50, 30)),
)
def test_retry_delay_is_capped(failures: int, delay: float) -> None:
    assert WallRuntime.retry_delay_seconds(failures, jitter=1.0) == delay


class _NamedNode:
    def __init__(self, name: str, parent: "_NamedNode | None" = None) -> None:
        self.name = name
        self.parent = parent

    def get_name(self) -> str:
        return self.name

    def get_parent(self) -> "_NamedNode | None":
        return self.parent


def test_error_source_is_mapped_through_feed_bin_ancestry() -> None:
    runtime = object.__new__(WallRuntime)
    runtime._feed_bins = {"feed_porch_4": ("porch", 4)}
    feed_bin = _NamedNode("feed_porch_4")
    decoder = _NamedNode("decode_porch", feed_bin)
    internal = _NamedNode("v4l2-internal", decoder)
    assert runtime._feed_identity_for_source(internal) == ("porch", 4)


def test_rotation_skips_unhealthy_feeds() -> None:
    runtime = object.__new__(WallRuntime)
    runtime.feeds = {
        "one": SimpleNamespace(state="healthy"),
        "two": SimpleNamespace(state="backoff"),
        "three": SimpleNamespace(state="healthy"),
    }
    viewport = SimpleNamespace(
        config=SimpleNamespace(feeds=("one", "two", "three")),
        active_index=0,
    )
    assert runtime._next_healthy_feed_index(viewport) == 2

    runtime.feeds["one"].state = "backoff"
    runtime.feeds["three"].state = "starting"
    assert runtime._next_healthy_feed_index(viewport) is None


def test_systemd_notification_uses_notify_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "notify.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(path)
    receiver.settimeout(1)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert WallRuntime._notify_systemd("WATCHDOG=1")
        assert receiver.recv(128) == b"WATCHDOG=1"
    finally:
        receiver.close()


class _FakeElement:
    """Record property writes so activation ordering can be asserted."""

    def __init__(self, name: str, log: list[tuple[str, str, object]]) -> None:
        self.name = name
        self.properties: dict[str, object] = {}
        self._log = log

    def get_name(self) -> str:
        return self.name

    def set_property(self, prop: str, value: object) -> None:
        self.properties[prop] = value
        self._log.append((self.name, prop, value))

    def find_property(self, prop: str) -> object | None:
        return object()

    def set_locked_state(self, locked: bool) -> None:
        self._log.append((self.name, "locked-state", locked))

    def sync_state_with_parent(self) -> bool:
        self._log.append((self.name, "sync-state", True))
        return True

    def set_state(self, state: object) -> str:
        self._log.append((self.name, "set-state", state))
        return "OK"

    def unlink(self, other: "_FakeElement") -> None:
        self._log.append((self.name, "unlink", other.get_name()))

    def link(self, other: "_FakeElement") -> bool:
        self._log.append((self.name, "link", other.get_name()))
        return True


class _FakeCaps:
    def __init__(self, text: str) -> None:
        self.text = text

    @classmethod
    def from_string(cls, text: str) -> "_FakeCaps":
        return cls(text)


def _aspect_runtime(render: PixelRect, crop: SourceCrop) -> tuple[WallRuntime, object]:
    log: list[tuple[str, str, object]] = []
    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(Caps=_FakeCaps)
    viewport = SimpleNamespace(
        config=SimpleNamespace(index=1, name="viewport1", feeds=("porch", "drive")),
        aspect=_FakeElement("aspect_upper_left", log),
        crop_values=crop,
        resolved=SimpleNamespace(render=render),
        pixel_aspect_ratio=None,
    )
    return runtime, viewport


def test_viewport_aspect_stretches_source_to_fill_destination() -> None:
    # A 480x360 doorbell feed must fill a 640x360 viewport rather than pillarbox.
    runtime, viewport = _aspect_runtime(
        PixelRect(x=0, y=0, width=639, height=359),
        SourceCrop(left=0, top=0, right=1, bottom=1),
    )
    runtime._apply_viewport_aspect(viewport, (480, 360))
    # destination 639x359 from a 479x359 crop -> par 639/479.
    assert viewport.pixel_aspect_ratio == Fraction(639, 479)
    assert viewport.aspect.properties["caps"].text == (
        "video/x-raw,pixel-aspect-ratio=639/479"
    )


def test_viewport_aspect_is_square_when_shapes_already_match() -> None:
    runtime, viewport = _aspect_runtime(
        PixelRect(x=0, y=0, width=640, height=360),
        SourceCrop(left=0, top=0, right=0, bottom=0),
    )
    runtime._apply_viewport_aspect(viewport, (640, 360))
    assert viewport.pixel_aspect_ratio == Fraction(1, 1)


def test_viewport_aspect_is_not_rewritten_when_unchanged() -> None:
    runtime, viewport = _aspect_runtime(
        PixelRect(x=0, y=0, width=639, height=359),
        SourceCrop(left=0, top=0, right=1, bottom=1),
    )
    runtime._apply_viewport_aspect(viewport, (640, 360))
    first = viewport.aspect.properties["caps"]
    runtime._apply_viewport_aspect(viewport, (640, 360))
    assert viewport.aspect.properties["caps"] is first


def test_viewport_aspect_ignores_an_unknown_source_size() -> None:
    runtime, viewport = _aspect_runtime(
        PixelRect(x=0, y=0, width=639, height=359),
        SourceCrop(left=0, top=0, right=1, bottom=1),
    )
    runtime._apply_viewport_aspect(viewport, None)
    assert viewport.pixel_aspect_ratio is None
    assert "caps" not in viewport.aspect.properties


class _FakePipeline:
    def __init__(self, log: list[tuple[str, str, object]]) -> None:
        self._log = log
        self.added: list[str] = []

    def add(self, element: _FakeElement) -> bool:
        self.added.append(element.get_name())
        self._log.append(("pipeline", "add", element.get_name()))
        return True

    def remove(self, element: _FakeElement) -> bool:
        self._log.append(("pipeline", "remove", element.get_name()))
        return True


def _activation_runtime(
    active_feed: str | None = "porch",
) -> tuple[WallRuntime, object, list[tuple[str, str, object]]]:
    log: list[tuple[str, str, object]] = []
    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(
        Caps=_FakeCaps,
        State=SimpleNamespace(NULL="NULL"),
        StateChangeReturn=SimpleNamespace(FAILURE="FAILURE"),
    )
    runtime.GstVideo = SimpleNamespace(
        VideoOverlay=SimpleNamespace(set_render_rectangle=lambda *a: True)
    )
    runtime.displays = {"main": SimpleNamespace(connector_id=35)}
    runtime.drm_fd = 0
    runtime.pipeline = _FakePipeline(log)
    runtime.feeds = {
        "porch": SimpleNamespace(state="healthy", source_size=(640, 360)),
        "drive": SimpleNamespace(state="healthy", source_size=(480, 360)),
    }
    viewport = SimpleNamespace(
        config=SimpleNamespace(index=1, name="viewport1", feeds=("porch", "drive")),
        selector=_FakeElement("selector_upper_left", log),
        aspect=_FakeElement("aspect_upper_left", log),
        valve=_FakeElement("valve_upper_left", log),
        output_queue=_FakeElement("output_queue_upper_left", log),
        sink=_FakeElement("kms_upper_left_0", log),
        plane_id=98,
        sink_generation=0,
        selector_pads={"porch": "pad_porch", "drive": "pad_drive"},
        crop_values=SourceCrop(left=0, top=0, right=1, bottom=1),
        resolved=SimpleNamespace(render=PixelRect(x=0, y=0, width=639, height=359)),
        pixel_aspect_ratio=None,
        active_index=0,
        active_feed=active_feed,
    )
    runtime.viewports = {"upper_left": viewport}
    # Track replacement sinks without opening DRM.
    def _new_sink(safe: str, plane_id: int, generation: int) -> _FakeElement:
        return _FakeElement(f"kms_{safe}_{generation}", log)

    runtime._new_kms_sink = _new_sink  # type: ignore[method-assign]
    return runtime, viewport, log


def test_activation_leaves_the_valve_open_at_the_end() -> None:
    # A closed valve stops the newly selected branch's ALLOCATION query from
    # reaching kmssink, which strands the decoder's DMA-BUF frames.
    runtime, viewport, log = _activation_runtime()
    runtime._activate_viewport_feed(viewport, 1)
    assert viewport.active_feed == "drive"
    assert viewport.active_index == 1
    assert viewport.valve.properties["drop"] is False
    assert [value for _, prop, value in log if prop == "drop"][-1] is False


def test_activation_sets_aspect_before_switching_the_pad() -> None:
    runtime, viewport, log = _activation_runtime()
    runtime._activate_viewport_feed(viewport, 1)
    order = [(name, prop) for name, prop, _ in log]
    assert order.index(("aspect_upper_left", "caps")) < order.index(
        ("selector_upper_left", "active-pad")
    )
    assert viewport.selector.properties["active-pad"] == "pad_drive"
    # The 480x360 feed is stretched across the same 639x359 destination.
    assert viewport.pixel_aspect_ratio == Fraction(639, 479)


def test_switching_feeds_keeps_the_same_kms_sink() -> None:
    # Replacing the sink on every switch blanked the plane for ~2s while the
    # new sink prerolled. Per-branch cropping keeps the negotiated pool valid,
    # so the plane can stay up across a switch.
    runtime, viewport, log = _activation_runtime(active_feed="porch")
    original = viewport.sink
    runtime._activate_viewport_feed(viewport, 1)
    assert viewport.sink is original
    assert viewport.sink_generation == 0
    assert not [e for e in log if e[1] == "remove"]


def test_switching_feeds_never_closes_the_valve() -> None:
    # A closed valve would drop frames and stall the newly selected branch's
    # allocation query; nothing should shut it mid-switch.
    runtime, viewport, log = _activation_runtime(active_feed="porch")
    runtime._activate_viewport_feed(viewport, 1)
    assert True not in [value for _, prop, value in log if prop == "drop"]
    assert viewport.valve.properties["drop"] is False


def test_reselecting_the_same_feed_keeps_the_sink() -> None:
    # A feed that merely became healthy again must not churn its KMS plane.
    runtime, viewport, log = _activation_runtime(active_feed="porch")
    original = viewport.sink
    runtime._activate_viewport_feed(viewport, 0)
    assert viewport.sink is original
    assert viewport.sink_generation == 0
    assert True not in [value for _, prop, value in log if prop == "drop"]


def test_first_activation_of_an_offline_viewport_keeps_its_primed_sink() -> None:
    # _show_viewport_offline already left a fresh sink behind; replacing it again
    # would discard the one the feed just negotiated against.
    runtime, viewport, log = _activation_runtime(active_feed=None)
    original = viewport.sink
    runtime._activate_viewport_feed(viewport, 1)
    assert viewport.sink is original
    assert viewport.sink_generation == 0
    assert viewport.active_feed == "drive"


def test_resource_error_explains_the_plane_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # kmsprint lists more free planes than the display controller can compose
    # at once, so this error looks like a missing plane when it is a viewport-count
    # problem.
    runtime = object.__new__(WallRuntime)
    runtime._fatal_error = None
    runtime.config = SimpleNamespace(viewports=("t",) * 9)
    runtime.stop = lambda: None
    with caplog.at_level("ERROR"):
        runtime._fatal("GStreamer encountered a general resource error.")
    assert "plane-budget" in caplog.text
    assert "9" in caplog.text


def test_unrelated_fatal_errors_stay_terse(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = object.__new__(WallRuntime)
    runtime._fatal_error = None
    runtime.config = SimpleNamespace(viewports=("t",) * 9)
    runtime.stop = lambda: None
    with caplog.at_level("ERROR"):
        runtime._fatal("could not open DRM device")
    assert "plane-budget" not in caplog.text


def _crop_viewport(*branch_crops: object) -> object:
    return SimpleNamespace(
        config=SimpleNamespace(index=1, name="viewport1", feeds=("porch",)),
        branch_crops={f"feed{i}": c for i, c in enumerate(branch_crops)},
        crop_values=None,
        resolved=SimpleNamespace(
            insets=SimpleNamespace(left=0, top=0, right=1, bottom=1)
        ),
    )


def test_every_branch_of_a_viewport_is_cropped_alike() -> None:
    # Each branch has its own videocrop upstream of the selector, so a rotating
    # viewport keeps its seam and its 1:1 plane. They must agree on the insets.
    log: list[tuple[str, str, object]] = []
    a = _FakeElement("crop_porch_to_upper_left", log)
    b = _FakeElement("crop_drive_to_upper_left", log)
    runtime = object.__new__(WallRuntime)
    viewport = _crop_viewport(a, b)
    runtime._apply_viewport_crop(viewport)
    expected = {"left": 0, "top": 0, "right": 1, "bottom": 1}
    assert a.properties == expected
    assert b.properties == expected
    assert viewport.crop_values == SourceCrop(left=0, top=0, right=1, bottom=1)


def test_viewport_crop_is_written_once() -> None:
    log: list[tuple[str, str, object]] = []
    element = _FakeElement("crop_porch_to_upper_left", log)
    runtime = object.__new__(WallRuntime)
    viewport = _crop_viewport(element)
    runtime._apply_viewport_crop(viewport)
    runtime._apply_viewport_crop(viewport)
    assert len([e for e in log if e[1] == "left"]) == 1


def test_a_viewport_with_no_seam_still_records_its_crop() -> None:
    runtime = object.__new__(WallRuntime)
    viewport = _crop_viewport()
    runtime._apply_viewport_crop(viewport)
    assert viewport.crop_values == SourceCrop(left=0, top=0, right=1, bottom=1)


def test_build_viewports_and_branches_construct_without_undefined_names() -> None:
    """Exercise real graph construction; the unit fakes never called these."""
    made: list[str] = []
    linked: list[tuple[str, str]] = []

    class _El:
        def __init__(self, name: str) -> None:
            self.name = name
            self.pads: dict[str, str] = {}

        def get_name(self) -> str:
            return self.name

        def set_property(self, *_a: object) -> None: ...
        def find_property(self, _p: str) -> object: return object()
        def get_static_pad(self, direction: str) -> "_Pad":
            return _Pad(f"{self.name}:{direction}")
        def request_pad_simple(self, template: str) -> "_Pad":
            return _Pad(f"{self.name}:{template}")
        def link(self, other: "_El") -> bool:
            linked.append((self.name, other.name))
            return True

    class _Pad:
        def __init__(self, name: str) -> None:
            self.name = name
        def link(self, other: "_Pad") -> str:
            linked.append((self.name, other.name))
            return "OK"
        def add_probe(self, *_a: object) -> int:
            return 1

    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(
        PadLinkReturn=SimpleNamespace(OK="OK"),
        PadProbeType=SimpleNamespace(BUFFER="BUFFER"),
        util_set_object_arg=lambda *a: None,
    )
    runtime.pipeline = SimpleNamespace(add=lambda e: made.append(e.get_name()))
    runtime.displays = {"main": SimpleNamespace(plane_ids=(98, 109), connector_id=35)}
    runtime._element = lambda factory, name: _El(name)
    runtime._new_kms_sink = lambda safe, plane, connector, gen: _El(
        f"kms_{safe}_{gen}"
    )
    runtime._add = lambda *els: [made.append(e.get_name()) for e in els]
    runtime._set_if_present = lambda *a: None
    runtime._set_object_arg_if_present = lambda *a: None
    runtime.config = SimpleNamespace(
        viewports=(
            SimpleNamespace(index=1, name="viewport1", feeds=("porch",), display="main"),
            SimpleNamespace(index=2, name="viewport2", feeds=("coop", "run"), display="main"),
        )
    )
    runtime.viewports = {}
    runtime._build_viewports()
    assert set(runtime.viewports) == {"viewport1", "viewport2"}

    runtime.feeds = {
        n: SimpleNamespace(tee=_El(f"tee_{n}")) for n in ("porch", "coop", "run")
    }
    runtime._connect_feed_branches()
    # Every branch of every viewport gets its own crop upstream of the selector.
    assert set(runtime.viewports["viewport2"].branch_crops) == {"coop", "run"}
    assert set(runtime.viewports["viewport1"].branch_crops) == {"porch"}
    assert any(name.startswith("crop_coop_to_viewport2") for name in made)


class _FakeStructure:
    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = fields

    def has_field(self, name: str) -> bool:
        return name in self._fields

    def get_string(self, name: str) -> str | None:
        return self._fields.get(name)


def test_srtp_detected_from_the_rfc4568_key_attribute() -> None:
    # rtspsrc surfaces an SDP "a=crypto" line as an "a-crypto" caps field.
    from viewwall.gst_runtime import _offers_srtp

    assert _offers_srtp(
        _FakeStructure(
            {
                "media": "video",
                "a-crypto": "1 AES_CM_128_HMAC_SHA1_80 inline:abc123",
            }
        )
    )


def test_srtp_detected_from_the_savp_profile() -> None:
    from viewwall.gst_runtime import _offers_srtp

    assert _offers_srtp(_FakeStructure({"profile": "RTP/SAVP"}))
    assert _offers_srtp(_FakeStructure({"profile": "RTP/SAVPF"}))


def test_plain_rtp_is_not_mistaken_for_srtp() -> None:
    from viewwall.gst_runtime import _offers_srtp

    assert not _offers_srtp(_FakeStructure({"media": "video", "profile": "RTP/AVP"}))
    assert not _offers_srtp(_FakeStructure({"media": "video"}))


def test_a_permanently_stopped_feed_is_not_restarted() -> None:
    # Retrying cannot fix an unsupported stream, so recovery must not revive it.
    runtime = object.__new__(WallRuntime)
    runtime._stopping = False
    runtime.feeds = {"cam": SimpleNamespace(generation=1, state="unsupported")}
    assert runtime._restart_feed("cam", 1, "some error") is False
    assert runtime.feeds["cam"].state == "unsupported"


def test_a_permanently_stopped_feed_is_not_marked_healthy() -> None:
    runtime = object.__new__(WallRuntime)
    runtime._stopping = False
    runtime.feeds = {"cam": SimpleNamespace(generation=1, state="unsupported")}
    assert runtime._mark_feed_healthy("cam", 1) is False
    assert runtime.feeds["cam"].state == "unsupported"


def test_kms_sinks_disable_qos() -> None:
    # QoS events from a vblank-limited sink make v4l2h264dec drop frames before
    # decoding, starving feeds to a fraction of their source rate.
    log: list[tuple[str, str, object]] = []
    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace()
    runtime.displays = {"main": SimpleNamespace(connector_id=35)}
    runtime.drm_fd = 0
    made = _FakeElement("kms_upper_left_0", log)
    runtime._element = lambda factory, name: made
    runtime._set_if_present = lambda el, prop, val: el.set_property(prop, val)
    sink = WallRuntime._new_kms_sink(runtime, "upper_left", 98, 35, 0)
    assert sink.properties["qos"] is False
    assert sink.properties["sync"] is True


def _watchdog_feed(starting_ms: int = 15_000) -> object:
    log: list[tuple[str, str, object]] = []
    element = _FakeElement("watchdog_cam", log)
    element.properties["timeout"] = starting_ms
    element.get_property = element.properties.get  # type: ignore[attr-defined]
    return SimpleNamespace(
        config=SimpleNamespace(name="cam"),
        watchdog=element,
        watchdog_reported=False,
    )


def test_watchdog_scales_to_a_slow_camera() -> None:
    # The bug this fixes: a 3fps camera tripped a fixed 15s watchdog for merely
    # being slow, turning a brief outage into minutes of retry backoff.
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    runtime._apply_feed_watchdog(feed, 3.0)
    assert feed.watchdog.properties["timeout"] == 15_000  # 45 frames at 3fps


def test_watchdog_tightens_for_a_fast_camera() -> None:
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    runtime._apply_feed_watchdog(feed, 30.0)
    # 45 frames at 30fps is 1.5s, floored by the minimum.
    assert feed.watchdog.properties["timeout"] == WallRuntime.MIN_STALL_TIMEOUT_MS


def test_a_watchdog_matching_the_default_is_still_reported(caplog) -> None:
    # 45 frames at 3fps is exactly the startup default, so the value never
    # changes and the feed logged nothing at all, indistinguishable from one
    # the scaling had never reached. The wall's 3fps camera looked like a bug
    # for that reason while being correctly protected the whole time.
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    with caplog.at_level("INFO"):
        runtime._apply_feed_watchdog(feed, 3.0)
    assert "stall watchdog set to 15.0s" in caplog.text
    assert feed.watchdog_reported


def test_an_unchanged_watchdog_is_reported_only_once(caplog) -> None:
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    runtime._apply_feed_watchdog(feed, 3.0)
    caplog.clear()
    with caplog.at_level("INFO"):
        runtime._apply_feed_watchdog(feed, 3.0)
    assert "stall watchdog" not in caplog.text


def test_watchdog_never_goes_below_the_floor() -> None:
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    runtime._apply_feed_watchdog(feed, 120.0)
    assert feed.watchdog.properties["timeout"] >= WallRuntime.MIN_STALL_TIMEOUT_MS


def test_the_derived_timeout_always_wins() -> None:
    # There is no configuration knob to override it: a fixed number a user
    # picks is strictly worse than one derived from the feed's own rate.
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed(starting_ms=60_000)
    runtime._apply_feed_watchdog(feed, 30.0)
    assert feed.watchdog.properties["timeout"] == 5_000


def test_watchdog_ignores_a_nonsense_framerate() -> None:
    runtime = object.__new__(WallRuntime)
    feed = _watchdog_feed()
    runtime._apply_feed_watchdog(feed, 0.0)
    assert feed.watchdog.properties["timeout"] == 15_000


def test_watchdog_handles_a_feed_with_no_bin_yet() -> None:
    runtime = object.__new__(WallRuntime)
    feed = SimpleNamespace(
        config=SimpleNamespace(name="cam", stall_timeout_ms=15_000), watchdog=None
    )
    runtime._apply_feed_watchdog(feed, 30.0)  # must not raise


def _observed_feed() -> object:
    log: list[tuple[str, str, object]] = []
    element = _FakeElement("watchdog_cam", log)
    element.properties["timeout"] = 15_000
    element.get_property = element.properties.get  # type: ignore[attr-defined]
    return SimpleNamespace(
        config=SimpleNamespace(name="cam", stall_timeout_ms=15_000),
        watchdog=element,
        last_frame_at=None,
        max_frame_gap=None,
        observed_fps_applied=False,
        caps_fps_known=False,
        watchdog_reported=False,
        decoded_frames=0,
        generation=0,
    )


def test_a_slow_feed_without_a_caps_framerate_is_measured() -> None:
    # The 3fps camera negotiates caps with no usable framerate, which is
    # exactly the case a fixed watchdog punishes.
    runtime = object.__new__(WallRuntime)
    feed = _observed_feed()
    for i in range(4):
        runtime._observe_feed_interval(feed, i / 3.0)   # 3fps
    assert feed.observed_fps_applied
    # 45 frames at 3fps is 15s.
    assert feed.watchdog.properties["timeout"] == 15_000


def test_a_very_slow_feed_gets_a_long_watchdog() -> None:
    runtime = object.__new__(WallRuntime)
    feed = _observed_feed()
    for i in range(3):
        runtime._observe_feed_interval(feed, i * 2.0)   # 0.5fps
    assert feed.watchdog.properties["timeout"] == 90_000


def test_a_fast_feed_is_not_measured_from_intervals() -> None:
    # Gaps under 0.2s (faster than 5fps) are already safe for a fixed watchdog.
    runtime = object.__new__(WallRuntime)
    feed = _observed_feed()
    for i in range(10):
        runtime._observe_feed_interval(feed, i / 30.0)
    assert not feed.observed_fps_applied
    assert feed.watchdog.properties["timeout"] == 15_000


def test_measurement_uses_the_worst_gap_not_the_latest() -> None:
    runtime = object.__new__(WallRuntime)
    feed = _observed_feed()
    runtime._observe_feed_interval(feed, 0.0)
    runtime._observe_feed_interval(feed, 2.0)    # a 2s gap
    assert feed.observed_fps_applied
    assert feed.watchdog.properties["timeout"] == 90_000


def test_a_declared_framerate_beats_measurement() -> None:
    # A fast feed that hiccups at startup must not have its watchdog loosened
    # by the measured gap; the caps rate is authoritative.
    runtime = object.__new__(WallRuntime)
    feed = _observed_feed()
    feed.caps_fps_known = True
    runtime.feeds = {"cam": feed}
    feed.generation = 1
    runtime.Gst = SimpleNamespace(PadProbeReturn=SimpleNamespace(OK="OK"))
    runtime._on_feed_buffer(None, None, ("cam", 1))
    assert not feed.observed_fps_applied


def _metrics_runtime(queue_ns: int | None = 45_000_000) -> WallRuntime:
    runtime = object.__new__(WallRuntime)
    runtime._stopping = False
    runtime._metrics_sampled_at = 0.0
    runtime._wall_dark = False
    queue = SimpleNamespace(
        get_property=lambda prop: queue_ns if prop == "current-level-time" else None
    )
    runtime.viewports = {
        "upper_left": SimpleNamespace(
            config=SimpleNamespace(index=1, name="viewport1", feeds=("porch",)),
            active_index=0,
            active_feed="porch",
            output_queue=queue if queue_ns is not None else None,
            rendered_frames=60,
            metrics_since=None,
            metrics_rotated=False,
        )
    }
    runtime.feeds = {
        "porch": SimpleNamespace(state="healthy", decoded_frames=61)
    }
    return runtime


def test_metrics_report_rates_over_the_elapsed_interval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _metrics_runtime()
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        assert runtime._report_metrics() is True
    record = caplog.records[-1]
    # 60 buffers over 2s, so 30fps; the fields carry numbers, not prose.
    assert record.VW_FPS == "30.0"
    assert record.VW_DECODED_FPS == "30.5"
    assert record.VW_QUEUE_MS == "45"
    assert record.VW_VIEWPORT == 1
    assert record.VW_STATE == "healthy"


def test_metrics_counters_reset_each_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise every interval would report the average since startup and a
    # dropout would be invisible.
    runtime = _metrics_runtime()
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    runtime._report_metrics()
    assert runtime.viewports["upper_left"].rendered_frames == 0
    assert runtime.feeds["porch"].decoded_frames == 0


def test_metrics_omit_queue_depth_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _metrics_runtime(queue_ns=None)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert not hasattr(caplog.records[-1], "VW_QUEUE_MS")


def test_metrics_stop_when_the_wall_is_stopping() -> None:
    runtime = _metrics_runtime()
    runtime._stopping = True
    assert runtime._report_metrics() is False


def test_a_primed_viewport_reports_its_feed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # _prime_offline_viewports_for_feed() opens a branch at startup without setting
    # active_feed, so a viewport that is rendering can still have it as None. The
    # report reads active_index, which is right in both paths.
    runtime = _metrics_runtime()
    runtime.viewports["upper_left"].active_feed = None
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    record = caplog.records[-1]
    assert record.VW_FEED == "porch"
    assert record.VW_FPS == "30.0"


def test_a_feed_in_two_viewports_reports_in_both(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Counters are read once up front; resetting inside the viewport loop would
    # leave whichever viewport came second reporting nothing.
    runtime = _metrics_runtime()
    runtime.viewports["lower_right"] = SimpleNamespace(
        config=SimpleNamespace(index=9, name="viewport9", feeds=("porch",)),
        active_index=0,
        active_feed="porch",
        output_queue=None,
        rendered_frames=30,
        metrics_since=None,
        metrics_rotated=False,
    )
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    decoded = [r.VW_DECODED_FPS for r in caplog.records[-2:]]
    assert decoded == ["30.5", "30.5"]


def test_a_viewport_measures_only_since_its_last_feed_switch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A viewport rotating a 3fps and a 24fps camera reported ~13fps: frames from
    # both feeds averaged over one interval. After a switch the viewport counts
    # from that moment, so the rate describes the feed actually selected.
    runtime = _metrics_runtime()
    viewport = runtime.viewports["upper_left"]
    viewport.rendered_frames = 45
    viewport.metrics_since = 0.5
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    # 45 frames in the 1.5s since the switch, not over the 2s interval.
    assert caplog.records[-1].VW_FPS == "30.0"


def test_a_rotated_viewport_is_flagged_as_not_comparable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # fps covers the seconds since the switch, decoded_fps the whole interval.
    # Comparing them would look like heavy frame loss, so the line says so.
    runtime = _metrics_runtime()
    runtime.viewports["upper_left"].metrics_since = 1.5
    runtime.viewports["upper_left"].metrics_rotated = True
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    record = caplog.records[-1]
    assert record.VW_ROTATED == "1"
    assert record.VW_WINDOW_S == "0.5"


def test_a_settled_viewport_is_not_flagged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A fixed viewport whose window is a few milliseconds short of the interval
    # must not be flagged; only an actual feed switch counts.
    runtime = _metrics_runtime()
    runtime.viewports["upper_left"].metrics_since = 0.01
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert not hasattr(caplog.records[-1], "VW_ROTATED")


class _FakeWarningMessage:
    def __init__(self, text: str, source: str, message_type: object) -> None:
        self._text = text
        self.src = _NamedNode(source)
        self.type = message_type

    def parse_warning(self) -> tuple[SimpleNamespace, str]:
        return SimpleNamespace(message=self._text), ""


def _bus_runtime() -> WallRuntime:
    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(
        MessageType=SimpleNamespace(
            ERROR="ERROR", ELEMENT="ELEMENT", EOS="EOS", WARNING="WARNING"
        )
    )
    return runtime


def test_the_late_buffer_warning_is_demoted(caplog: pytest.LogCaptureFixture) -> None:
    # GstBaseSink emits this from a late-buffer count alone, so a decoder that
    # releases frames in bursts trips it continuously while losing nothing. It
    # was the only warning the wall ever produced.
    runtime = _bus_runtime()
    message = _FakeWarningMessage(
        "A lot of buffers are being dropped.", "kms_upper_right_0", "WARNING"
    )
    with caplog.at_level("DEBUG", logger="viewwall.gst_runtime"):
        runtime._on_bus_message(None, message)
    record = caplog.records[-1]
    assert record.levelname == "DEBUG"
    # Still says which sink, so it remains usable at --log-level DEBUG.
    assert "kms_upper_right_0" in record.getMessage()


def test_other_gstreamer_warnings_stay_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _bus_runtime()
    message = _FakeWarningMessage("Could not read from resource.", "rtsp_porch", "WARNING")
    with caplog.at_level("DEBUG", logger="viewwall.gst_runtime"):
        runtime._on_bus_message(None, message)
    assert caplog.records[-1].levelname == "WARNING"


def test_an_unmeasurably_short_window_reports_no_rate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A rotation landing microseconds before the report once produced
    # "fps=416.5 window_s=0.0": one stray buffer divided by almost no time.
    runtime = _metrics_runtime()
    viewport = runtime.viewports["upper_left"]
    viewport.rendered_frames = 1
    viewport.metrics_since = 1.998
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    record = caplog.records[-1]
    assert record.VW_FPS == "-"
    # The raw count still shows the viewport is alive.
    assert record.VW_FRAMES == "1"


def test_a_full_window_reports_no_frame_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _metrics_runtime()
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert not hasattr(caplog.records[-1], "VW_FRAMES")


def _dark_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[WallRuntime, list[str]]:
    sent: list[str] = []
    runtime = _metrics_runtime()
    runtime.viewports["upper_left"].rendered_frames = 0
    monkeypatch.setattr(
        WallRuntime, "_notify_systemd", staticmethod(lambda m: sent.append(m) or True)
    )
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    return runtime, sent


def test_a_wall_showing_nothing_is_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # One black viewport is ordinary; every viewport black is the NVR being gone, and
    # used to be reported to systemd as "Camera wall running" indefinitely.
    runtime, sent = _dark_runtime(monkeypatch)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert any("No video" in m for m in sent)


def test_the_dark_wall_error_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    # A long outage should be one error, not one per interval.
    runtime, sent = _dark_runtime(monkeypatch)
    runtime._report_metrics()
    runtime._metrics_sampled_at = 0.0
    runtime._report_metrics()
    assert len([m for m in sent if "No video" in m]) == 1


def test_recovery_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime, sent = _dark_runtime(monkeypatch)
    runtime._report_metrics()
    runtime.viewports["upper_left"].rendered_frames = 60
    runtime._metrics_sampled_at = 0.0
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert runtime._wall_dark is False
    assert any("video restored" in r.getMessage() for r in caplog.records)
    assert sent[-1] == "STATUS=Camera wall running"


def test_a_healthy_wall_reports_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sent = _dark_runtime(monkeypatch)
    runtime.viewports["upper_left"].rendered_frames = 60
    runtime._report_metrics()
    assert sent == []


def test_every_attribute_the_callbacks_use_is_initialised() -> None:
    """Guard the gap that shipped a crashing metrics timer.

    The metrics tests build a runtime with object.__new__ and set attributes by
    hand, so they cannot notice one missing from __init__. _wall_dark was, and
    the first _report_metrics tick died with AttributeError -- which GLib
    swallows by silently removing the timer, so metrics stopped after one
    interval and nothing said why.

    Parsing __init__ for self.X assignments is crude, but it compares the two
    lists that drifted apart.
    """
    import ast
    import inspect

    source = inspect.getsource(WallRuntime.__init__)
    tree = ast.parse(source.strip())
    assigned = set()
    for node in ast.walk(tree):
        # Both "self.x = ..." and the annotated "self.x: T = ...".
        targets = getattr(node, "targets", None) or (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                assigned.add(target.attr)
    for required in ("_wall_dark", "_metrics_sampled_at", "_stopping", "_fatal_error"):
        assert required in assigned, f"{required} is used but never initialised"
