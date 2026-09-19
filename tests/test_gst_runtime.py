import logging
import socket
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from viewwall.display import DisplayState
from viewwall.gst_runtime import RuntimeDependencyError, WallRuntime
from viewwall.config import LayoutConfig, RectSpec, ViewportConfig
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
        # kms-planes: its own sink, no compositor pad.
        mixer_pad=None,
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
        # kms-planes: its own sink, no compositor pad.
        mixer_pad=None,
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
        # kms-planes: its own sink, no compositor pad.
        mixer_pad=None,
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
        output_total=0,
        rate_sample=(1, 2),
        keeping_up_at=100.0,
        rate_shown=0,
        rate_decoded=0,
    )
    runtime.viewports = {"upper_left": viewport}
    # Track replacement sinks without opening DRM.
    def _new_sink(safe: str, plane_id: int, generation: int) -> _FakeElement:
        return _FakeElement(f"kms_{safe}_{generation}", log)

    runtime._new_kms_sink = _new_sink  # type: ignore[method-assign]
    return runtime, viewport, log


def test_activation_drops_the_rate_baseline_but_keeps_the_dwell() -> None:
    # The baseline pairs this viewport's output total with the *outgoing*
    # feed's decoded total, so carrying it across a switch makes the next
    # interval subtract unrelated counters and read negative -- which passes
    # the floor test and silently restarts the dwell. The dwell itself has to
    # survive, or a viewport rotating every eight seconds could never reach
    # the thirty it needs, which is the viewport the fault was seen on.
    runtime, viewport, _log = _activation_runtime()
    runtime._activate_viewport_feed(viewport, 1)
    assert viewport.rate_sample is None
    assert viewport.keeping_up_at == 100.0


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
        PadProbeType=SimpleNamespace(
            BUFFER="BUFFER", EVENT_DOWNSTREAM="EVENT_DOWNSTREAM"
        ),
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
        ),
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


def _compositing_runtime(made, linked, el_factory, background="#000000"):
    """A runtime wired for the compositor backend, sharing the build fakes."""
    runtime = object.__new__(WallRuntime)
    runtime.compositing = True
    runtime.drm_fd = 7
    runtime.Gst = SimpleNamespace(
        PadLinkReturn=SimpleNamespace(OK="OK"),
        PadProbeType=SimpleNamespace(
            BUFFER="BUFFER", EVENT_DOWNSTREAM="EVENT_DOWNSTREAM"
        ),
        util_set_object_arg=lambda *a: None,
        Caps=SimpleNamespace(from_string=lambda text: text),
    )
    runtime.pipeline = SimpleNamespace(add=lambda e: made.append(e.get_name()))
    runtime.displays = {
        "main": SimpleNamespace(
            plane_ids=(98, 109), connector_id=35, width=1920, height=1080
        )
    }
    runtime._built = []

    def _make(factory, name):
        element = el_factory(name)
        runtime._built.append(element)
        return element

    runtime._element = _make
    runtime._add = lambda *els: [made.append(e.get_name()) for e in els]
    runtime._link_many = lambda *els: [
        linked.append((a.get_name(), b.get_name()))
        for a, b in zip(els, els[1:])
    ]
    # Real _set_if_present writes the property when the element has it; the
    # fakes all claim to, so honour it rather than silently dropping writes
    # the tests then cannot see.
    runtime._set_if_present = lambda obj, key, value: obj.set_property(key, value)
    runtime._set_object_arg_if_present = lambda obj, key, value: obj.set_property(
        key, value
    )
    runtime._on_viewport_buffer = lambda *a: None
    runtime.config = SimpleNamespace(
        drm=SimpleNamespace(background=background),
        viewports=(
            SimpleNamespace(index=1, name="viewport1", feeds=("porch",), display="main"),
            SimpleNamespace(
                index=2, name="viewport2", feeds=("coop", "run"), display="main"
            ),
        ),
    )
    runtime.viewports = {}
    runtime.composited_sinks = {}
    runtime.composited_caps = {}
    runtime._composited_totals = {}
    return runtime


def test_compositor_backend_builds_one_sink_for_the_whole_display() -> None:
    """Nine viewports, one commit: the point of the backend."""
    made: list[str] = []
    linked: list[tuple[str, str]] = []

    class _El:
        def __init__(self, name): self.name = name
        def get_name(self): return self.name
        def set_property(self, *_a): ...
        def find_property(self, _p): return object()
        def get_static_pad(self, d): return _Pad(f"{self.name}:{d}")
        def request_pad_simple(self, t): return _Pad(f"{self.name}:{t}")
        def link(self, other):
            linked.append((self.name, other.name))
            return True

    class _Pad:
        def __init__(self, name): self.name = name
        def link(self, other):
            linked.append((self.name, other.name))
            return "OK"
        def set_property(self, *_a): ...
        def add_probe(self, *_a): return 1

    runtime = _compositing_runtime(made, linked, _El)
    runtime._build_viewports()

    assert set(runtime.viewports) == {"viewport1", "viewport2"}
    # Exactly one kmssink and one compositor, however many viewports.
    assert sum(1 for n in made if n.startswith("kms_")) == 1
    assert sum(1 for n in made if n.startswith("compositor_main")) == 1
    # No viewport owns a sink; each owns a compositor pad instead.
    for viewport in runtime.viewports.values():
        assert viewport.sink is None
        assert viewport.mixer_pad is not None


def test_compositor_viewports_get_distinct_pads() -> None:
    """Two viewports sharing one pad would stack them in the same tile."""
    made: list[str] = []
    linked: list[tuple[str, str]] = []

    class _El:
        def __init__(self, name):
            self.name = name
            self.requested = 0

        def get_name(self): return self.name
        def set_property(self, *_a): ...
        def find_property(self, _p): return object()
        def get_static_pad(self, d): return _Pad(f"{self.name}:{d}")

        def request_pad_simple(self, t):
            # Real request pads are numbered as they are handed out; a fake
            # returning one name for every call would hide a collision.
            self.requested += 1
            return _Pad(f"{self.name}:{t}#{self.requested}")

        def link(self, other): return True

    class _Pad:
        def __init__(self, name): self.name = name
        def link(self, other): return "OK"
        def set_property(self, *_a): ...
        def add_probe(self, *_a): return 1

    runtime = _compositing_runtime(made, linked, _El)
    runtime._build_viewports()
    pads = {v.mixer_pad.name for v in runtime.viewports.values()}
    assert len(pads) == len(runtime.viewports)


def _compositor_fakes(made, linked):
    pads: list = []

    class _El:
        def __init__(self, name):
            self.name = name
            self.props: dict[str, object] = {}
            self.requested = 0

        def get_name(self): return self.name
        def set_property(self, key, value): self.props[key] = value
        def find_property(self, _p): return object()
        def get_static_pad(self, d): return _Pad(f"{self.name}:{d}")

        def request_pad_simple(self, t):
            self.requested += 1
            p = _Pad(f"{self.name}:{t}#{self.requested}")
            pads.append(p)
            return p

        def link(self, other):
            linked.append((self.name, other.name))
            return True

    class _Pad:
        def __init__(self, name):
            self.name = name
            self.props: dict[str, object] = {}

        def link(self, other):
            linked.append((self.name, other.name))
            return "OK"

        def set_property(self, key, value): self.props[key] = value
        def add_probe(self, *_a): return 1

    _El.pads = pads
    return _El, _Pad


def test_compositor_paints_the_configured_background() -> None:
    """drm.background is the wall's setting; the compositor must honour it."""
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el, background="#102030")
    runtime._build_viewports()
    source = next(
        e for e in runtime._built if e.get_name().startswith("compositor_bg_src_main")
    )
    # The colour itself, not merely that a source was built: videotestsrc
    # takes 0xAARRGGBB and reads a zero alpha byte as fully transparent, so
    # the opaque byte has to be there too.
    assert source.props["foreground-color"] == 0xFF102030
    assert source.props["pattern"] == "solid-color"


def test_compositor_refuses_background_none(tmp_path) -> None:
    """"none" shows the console through the gaps, and there is no console
    behind a full-screen composited frame. Refused in configuration rather
    than reinterpreted into something else at runtime."""
    from viewwall.config import ConfigError, load_config

    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
output_backend = "compositor"
background = "none"

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
    with pytest.raises(ConfigError, match="kms-planes"):
        load_config(config_path, {})


def test_kms_planes_still_allows_background_none(tmp_path) -> None:
    """The setting keeps its meaning on the backend that can honour it."""
    from viewwall.config import load_config

    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(
        """
[drm]
background = "none"

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
    assert load_config(config_path, {}).drm.background is None


def test_compositor_background_sits_below_the_viewports() -> None:
    """Otherwise a full-screen background hides every tile."""
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el, background="#000000")
    runtime._build_viewports()
    # Both halves of the invariant: the viewports above, and the background
    # below them. Asserting only the viewports would pass with the background
    # at any zorder at all, including one that hides the whole wall.
    for viewport in runtime.viewports.values():
        assert viewport.mixer_pad.props["zorder"] == 1
    bg_pads = [p for p in el.pads if p.props.get("zorder") == 0]
    assert len(bg_pads) == 1, "the background pad must sit at zorder 0"


def test_compositor_keeps_its_capsfilter_for_a_mode_change() -> None:
    """The buffer size has to move with the rectangle, or the wall stretches.

    _apply_layout retargets both when a display changes resolution. It reads
    the capsfilter out of composited_caps, so a compositor that built one and
    dropped it turned that repair into a silent no-op: the rectangle moved,
    the compositor kept producing the old resolution into it, and the wall
    showed its top-left corner blown up to fill the screen.
    """
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el, background="#000000")
    runtime._build_viewports()

    caps = runtime.composited_caps.get("main")
    assert caps is not None, "the compositor must keep its capsfilter"
    # Built at the display's current size...
    assert "width=1920" in caps.props["caps"].replace(" ", "")
    assert "height=1080" in caps.props["caps"].replace(" ", "")

    # ...and retargeted when the mode changes under it.
    runtime.displays["main"] = SimpleNamespace(
        plane_ids=(98, 109), connector_id=35, width=1280, height=720
    )
    runtime.GstVideo = SimpleNamespace(
        VideoOverlay=SimpleNamespace(set_render_rectangle=lambda *_a: True)
    )
    runtime.config.displays = (
        SimpleNamespace(name="main", gap_px=0, outer_margin_px=0),
    )
    runtime.config.layout_for = lambda _d: LayoutConfig(gap_px=0, outer_margin_px=0)
    real = tuple(
        ViewportConfig(
            index=i,
            rect=RectSpec(
                x=Fraction(0), y=Fraction(0), width=Fraction(1), height=Fraction(1)
            ),
            feeds=v.feeds,
            display="main",
        )
        for i, v in enumerate(runtime.config.viewports, 1)
    )
    runtime.config.viewports_for = lambda _d: real
    runtime.feeds = {
        f: SimpleNamespace(source_size=(640, 360))
        for v in runtime.config.viewports
        for f in v.feeds
    }
    runtime._apply_layout("main")
    assert "width=1280" in caps.props["caps"].replace(" ", "")
    assert "height=720" in caps.props["caps"].replace(" ", "")


def test_compositor_background_does_not_pin_a_framerate() -> None:
    """compositor emits at its slowest input's rate.

    A framerate on the background -- the 1/2 that _build_background() uses for
    a static modeset frame -- throttled the whole wall to 0.5 fps on hardware.
    """
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el, background="#000000")
    runtime._build_viewports()
    bg_caps = next(
        e for e in runtime._built if e.get_name().startswith("compositor_bg_caps")
    )
    assert "framerate" not in bg_caps.props["caps"]


def test_compositor_forces_square_pixels() -> None:
    """A camera's PAR has nothing to say once pad geometry places the source.

    A Unifi G3 Dome advertises 189/190, which letterboxed ~3px of background
    down the right of its tile. Applied to every viewport, not just that one.
    """
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el, background="#000000")
    runtime._build_viewports()
    aspects = [e for e in runtime._built if e.get_name().startswith("aspect_")]
    assert aspects
    for aspect in aspects:
        assert "pixel-aspect-ratio=1/1" in aspect.props["caps"].replace(" ", "")


def test_composited_rate_is_reported_per_display(caplog) -> None:
    """The per-viewport lines say what each tile handed the compositor; this
    is the other half -- what the one sink actually scanned out."""
    import logging

    runtime = object.__new__(WallRuntime)
    runtime.compositing = True
    runtime.composited_sinks = {"main": object()}
    runtime._composited_totals = {"main": (100, 2)}
    runtime._sink_presented = lambda _sink: (250, 5)
    with caplog.at_level(logging.INFO, logger="viewwall.gst_runtime"):
        runtime._report_composited_rate(10.0)
    line = "".join(r.getMessage() for r in caplog.records)
    # 150 frames over 10s.
    assert "composited_fps=15.0" in line.replace("VW_COMPOSITED_FPS", "composited_fps")
    assert runtime._composited_totals["main"] == (250, 5)


def test_composited_rate_is_silent_under_kms_planes() -> None:
    """There is no single composited frame to measure under that backend."""
    runtime = object.__new__(WallRuntime)
    runtime.compositing = False
    runtime.composited_sinks = {}
    runtime.composited_caps = {}
    runtime._composited_totals = {}
    runtime._report_composited_rate(10.0)  # must not raise


def test_plane_demand_collapses_for_the_compositor_everywhere() -> None:
    """The display poll re-probes on every resolution change.

    It computed its own demand and did not know about the backend, so a
    nine-viewport wall asked for nine overlay planes it never uses -- enough
    to fail the probe on a Pi 3, where five is the practical limit.
    """
    runtime = object.__new__(WallRuntime)
    runtime.compositing = True
    runtime.config = SimpleNamespace(
        displays=(SimpleNamespace(name="main"),),
        viewports_for=lambda display: (1, 2, 3, 4, 5, 6, 7, 8, 9),
    )
    # Both call sites: startup passes counts, the poll passes nothing.
    assert runtime._plane_demand({"main": 9}) == {"main": 1}
    assert runtime._plane_demand() == {"main": 1}

    runtime.compositing = False
    assert runtime._plane_demand({"main": 9}) == {"main": 9}
    assert runtime._plane_demand() == {"main": 9}


def test_a_dead_tile_leaves_the_compositor_blend() -> None:
    """Closing the valve is not enough under the compositor.

    compositor keeps blending the last buffer it received into every output
    frame, so a dead camera froze on its final picture -- which reads as a
    working camera, worse than a black tile.
    """
    runtime = object.__new__(WallRuntime)
    runtime.compositing = True
    pad = SimpleNamespace(props={}, set_property=lambda k, v: pad.props.__setitem__(k, v))
    viewport = SimpleNamespace(
        mixer_pad=pad,
        sink=None,
        active_feed="porch",
        valve=SimpleNamespace(set_property=lambda *a: None),
        config=SimpleNamespace(index=1, feeds=("porch",)),
    )
    runtime._show_viewport_offline(viewport)
    assert pad.props["alpha"] == 0.0


def test_the_compositor_crops_for_its_seam_too() -> None:
    """The gap is cut from the source, not drawn over the output.

    Without the crop a 640x360 feed lands in a 639x359 destination, which is
    a resize of every frame of every tile: measured on a Pi 5, that put 63%
    of the process into Orc and libgstvideo and took the wall from 58% of a
    core to 127%. With it the source is 639x359 and lands 1:1.
    """
    made: list[str] = []
    linked: list[tuple[str, str]] = []
    el, pad = _compositor_fakes(made, linked)
    runtime = _compositing_runtime(made, linked, el)
    runtime._build_viewports()
    runtime.feeds = {
        name: SimpleNamespace(tee=el(f"tee_{name}"), config=SimpleNamespace(name=name))
        for name in ("porch", "coop", "run")
    }
    runtime.pipeline.add = lambda e: made.append(e.get_name())
    runtime._connect_feed_branches()
    assert any(n.startswith("crop_") for n in made)
    for viewport in runtime.viewports.values():
        assert viewport.branch_crops, "a composited viewport needs its crop"


def test_kms_planes_stays_the_default() -> None:
    """The shipped zero-copy path must not change under anyone's feet."""
    from viewwall.config import DrmConfig

    assert DrmConfig().output_backend == "kms-planes"
    # Deliberately not asserting WallRuntime.compositing: it is set in
    # __init__, and the class-level declaration is None so that a path
    # reading it on a half-built runtime raises rather than quietly
    # behaving as kms-planes.


def _modes_runtime(mode, background="#000000", probed=(1920, 1080)):
    runtime = object.__new__(WallRuntime)
    runtime.displays = {
        "main": DisplayState(
            connector_id=35,
            crtc_index=3,
            crtc_id=97,
            width=probed[0],
            height=probed[1],
            plane_ids=(98, 109),
        )
    }
    runtime.config = SimpleNamespace(
        displays=(SimpleNamespace(name="main", mode=mode),),
        drm=SimpleNamespace(background=background),
    )
    return runtime


def test_unsupported_mode_is_refused_by_name(monkeypatch) -> None:
    """Otherwise kmssink fails with "Internal data stream error".

    That names neither the mode nor the option that chose it, and the wall
    comes up laid out for a size the screen never reaches.
    """
    runtime = _modes_runtime((1234, 567))
    monkeypatch.setattr(
        "viewwall.gst_runtime.available_modes",
        lambda cid: {(1920, 1080), (1280, 720)},
    )
    with pytest.raises(RuntimeDependencyError, match="1234x567 is not offered"):
        runtime._apply_configured_modes()


def test_supported_mode_passes(monkeypatch) -> None:
    runtime = _modes_runtime((1280, 720))
    monkeypatch.setattr(
        "viewwall.gst_runtime.available_modes",
        lambda cid: {(1920, 1080), (1280, 720)},
    )
    runtime._apply_configured_modes()
    assert (runtime.displays["main"].width, runtime.displays["main"].height) == (
        1280,
        720,
    )


def test_mode_is_not_rejected_when_sysfs_says_nothing(monkeypatch) -> None:
    # An empty set means unknown, not unsupported.
    runtime = _modes_runtime((1280, 720))
    monkeypatch.setattr("viewwall.gst_runtime.available_modes", lambda cid: set())
    runtime._apply_configured_modes()
    assert runtime.displays["main"].width == 1280


def test_background_errors_do_not_kill_the_wall(caplog) -> None:
    """The one element the wall does not need."""
    runtime = object.__new__(WallRuntime)
    runtime.background_sinks = {"main": object()}
    assert runtime._is_background_source(
        SimpleNamespace(get_name=lambda: "background_src_main")
    )
    assert runtime._is_background_source(
        SimpleNamespace(get_name=lambda: "kms_background_main")
    )
    # A viewport sink is not the background and must stay fatal.
    assert not runtime._is_background_source(
        SimpleNamespace(get_name=lambda: "kms_viewport3_0")
    )
    runtime.background_sinks = {}
    assert not runtime._is_background_source(
        SimpleNamespace(get_name=lambda: "background_src_main")
    )


def test_configured_mode_replaces_the_probed_size() -> None:
    """Rectangles are computed from these numbers before the mode is set."""
    runtime = _modes_runtime((1280, 720))
    runtime._apply_configured_modes()
    state = runtime.displays["main"]
    assert (state.width, state.height) == (1280, 720)
    # Only the size changes; the plane and connector identity does not.
    assert state.plane_ids == (98, 109)
    assert state.connector_id == 35
    assert state.crtc_id == 97


def test_no_mode_keeps_what_was_probed() -> None:
    runtime = _modes_runtime(None)
    runtime._apply_configured_modes()
    assert (runtime.displays["main"].width, runtime.displays["main"].height) == (
        1920,
        1080,
    )


def test_mode_matching_the_probe_is_not_reported(caplog) -> None:
    runtime = _modes_runtime((1920, 1080))
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._apply_configured_modes()
    assert "mode" not in caplog.text


def test_mode_applies_without_a_background_colour() -> None:
    """The sink is a vehicle for the modeset; painting is the other job."""
    runtime = _modes_runtime((1280, 720), background=None)
    runtime._apply_configured_modes()
    state = runtime.displays["main"]
    assert (state.width, state.height) == (1280, 720)


def _background_runtime(colour: str | None, *, missing: bool = False, mode=None):
    """A runtime stripped to what _build_background touches."""
    made: list[str] = []
    props: dict[str, object] = {}

    class _El:
        def __init__(self, name: str) -> None:
            self.name = name

        def get_name(self) -> str:
            return self.name

        def set_property(self, prop: str, value: object) -> None:
            props[f"{self.name}.{prop}"] = value

        def link(self, other: "_El") -> bool:
            return True

    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(
        Caps=SimpleNamespace(from_string=lambda text: text)
    )
    runtime.drm_fd = 0
    runtime.displays = {
        "main": SimpleNamespace(connector_id=35, width=1920, height=1080)
    }
    runtime.config = SimpleNamespace(
        drm=SimpleNamespace(background=colour),
        displays=(SimpleNamespace(name="main", mode=mode),),
    )
    runtime.background_sinks = {}

    def _element(factory: str, name: str):
        if missing:
            raise RuntimeDependencyError(f"missing element: {factory}")
        return _El(name)

    runtime._element = _element
    runtime._add = lambda *els: [made.append(e.get_name()) for e in els]
    runtime._link_many = lambda *els: None
    runtime._set_if_present = lambda el, prop, value: props.__setitem__(
        f"{el.get_name()}.{prop}", value
    )
    return runtime, made, props


def test_background_paints_the_configured_colour() -> None:
    runtime, made, props = _background_runtime("#204060")
    runtime._build_background()
    assert set(runtime.background_sinks) == {"main"}
    assert any(name.startswith("background_src_") for name in made)
    # Alpha must be set or videotestsrc reads the colour as transparent.
    assert props["background_src_main.foreground-color"] == 0xFF204060


def test_background_uses_modesetting_rather_than_a_plane() -> None:
    """The distinction the whole feature rests on.

    A full-screen overlay plane costs about six viewports of VC4 HVS budget
    and fails with ENOSPC beside a real wall. A modeset costs nothing. Left
    to itself kmssink would take the first free overlay, which is a plane a
    viewport needs, so nothing here may set plane-id either.
    """
    runtime, _made, props = _background_runtime("#000000")
    runtime._build_background()
    assert props["kms_background_main.force-modesetting"] is True
    assert not any(key.endswith(".plane-id") for key in props)


def test_background_source_stays_live() -> None:
    # A single buffer would revert the moment the element left PLAYING.
    runtime, _made, props = _background_runtime("#000000")
    runtime._build_background()
    assert props["background_src_main.is-live"] is True


def test_mode_only_builds_the_sink_without_a_colour() -> None:
    """background = "none" with a mode still needs the modeset element."""
    runtime, made, props = _background_runtime(None, mode=(1280, 720))
    runtime._build_background()
    assert set(runtime.background_sinks) == {"main"}
    assert props["kms_background_main.force-modesetting"] is True
    # Opaque black behind tiles that cover it anyway; the alpha byte still
    # has to be set or videotestsrc reads the colour as transparent.
    assert props["background_src_main.foreground-color"] == 0xFF000000


def test_mode_only_says_so_rather_than_naming_a_colour(caplog) -> None:
    runtime, _made, _props = _background_runtime(None, mode=(1280, 720))
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._build_background()
    assert "modeset only" in caplog.text


def test_background_none_builds_nothing() -> None:
    runtime, made, _props = _background_runtime(None)
    runtime._build_background()
    assert runtime.background_sinks == {}
    assert made == []


def test_background_failure_does_not_stop_the_wall(caplog) -> None:
    """A wall showing the console beats no wall at all."""
    runtime, _made, _props = _background_runtime("#000000", missing=True)
    with caplog.at_level(logging.WARNING):
        runtime._build_background()
    assert runtime.background_sinks == {}
    assert "background unavailable" in caplog.text


def _stats_sink(rendered: int, dropped: int = 0):
    stats = SimpleNamespace(
        get_value=lambda key: {"rendered": rendered, "dropped": dropped}[key]
    )
    return SimpleNamespace(get_property=lambda prop: stats if prop == "stats" else None)


def test_presented_fps_is_the_delta_between_reports() -> None:
    """kmssink reports totals, so a rate needs the previous reading."""
    runtime = object.__new__(WallRuntime)
    assert runtime._sink_presented(_stats_sink(120, 3)) == (120, 3)


def test_presented_fps_survives_a_sink_without_stats() -> None:
    # Not every build carries the property, and a missing counter must not
    # take down the report that carries every other field.
    runtime = object.__new__(WallRuntime)
    assert runtime._sink_presented(None) is None
    assert runtime._sink_presented(SimpleNamespace()) is None
    assert (
        runtime._sink_presented(SimpleNamespace(get_property=lambda p: None)) is None
    )


def test_presented_fps_reported_next_to_rendered(caplog, monkeypatch) -> None:
    runtime = _metrics_runtime()
    viewport = runtime.viewports["upper_left"]
    # 60 rendered but only 12 presented: the plane path is the bottleneck,
    # which is the whole reason the field exists.
    viewport.sink = _stats_sink(12)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert "presented_fps=6.0" in caplog.text
    assert viewport.presented_total == 12


def test_presented_fps_restarts_with_a_replaced_sink(caplog, monkeypatch) -> None:
    """A replaced sink counts from zero, which is not a counter wrap."""
    runtime = _metrics_runtime()
    viewport = runtime.viewports["upper_left"]
    viewport.presented_total = 500
    viewport.sink = _stats_sink(4)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    # No rate this interval rather than a negative one, and the baseline
    # follows the new sink so the next interval reads correctly.
    assert "presented_fps=" not in caplog.text
    assert viewport.presented_total == 4


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
        decoded_total=0,
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


def test_queue_level_reports_the_buffer_count() -> None:
    # The decisive number when a rotating viewport is slow to change picture.
    # current-level-time on a buffer-bounded queue is just the count divided
    # by the feed rate, so it reads about the same whether the queue is full
    # or empty; the count itself does not.
    runtime = object.__new__(WallRuntime)
    queue = SimpleNamespace(
        get_property=lambda prop: 32 if prop == "current-level-buffers" else None
    )
    assert runtime._queue_level_buffers(queue) == 32


def test_queue_level_is_absent_when_the_queue_cannot_say() -> None:
    runtime = object.__new__(WallRuntime)
    assert runtime._queue_level_buffers(None) is None
    unreadable = SimpleNamespace(get_property=lambda prop: None)
    assert runtime._queue_level_buffers(unreadable) is None


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
            queued_frames=60,
            metrics_since=None,
            metrics_rotated=False,
            sink=None,
            presented_total=0,
            dropped_total=0,
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
    assert record.VW_QUEUED_FPS == "30.0"
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
    assert runtime.viewports["upper_left"].queued_frames == 0
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
    assert record.VW_QUEUED_FPS == "30.0"


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
        queued_frames=30,
        metrics_since=None,
        metrics_rotated=False,
        sink=None,
        presented_total=0,
        dropped_total=0,
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
    viewport.queued_frames = 45
    viewport.metrics_since = 0.5
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    # 45 frames in the 1.5s since the switch, not over the 2s interval.
    assert caplog.records[-1].VW_QUEUED_FPS == "30.0"


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
    viewport.queued_frames = 1
    viewport.metrics_since = 1.998
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 2.0)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    record = caplog.records[-1]
    assert record.VW_QUEUED_FPS == "-"
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
    runtime.viewports["upper_left"].queued_frames = 0
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
    runtime.viewports["upper_left"].queued_frames = 60
    runtime._metrics_sampled_at = 0.0
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._report_metrics()
    assert runtime._wall_dark is False
    assert any("video restored" in r.getMessage() for r in caplog.records)
    assert sent[-1] == "STATUS=Camera wall running"


def test_a_healthy_wall_reports_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sent = _dark_runtime(monkeypatch)
    runtime.viewports["upper_left"].queued_frames = 60
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


def _lifetime_feed() -> object:
    return SimpleNamespace(
        config=SimpleNamespace(name="cam"),
        healthy_at=None,
        branches_rebuilt=False,
        fatal_at=None,
        generation=1,
    )


STALL = "error from watchdog_cam: Watchdog triggered"
SOURCE = "error from rtsp_cam: Could not read from resource."


def _died(runtime, feed, reason: str = STALL, stalled: bool = True) -> None:
    """One generation ending, the way _restart_feed reports it."""
    runtime._note_generation_lifetime(feed, reason, stalled)


def _connecting_feed(**over):
    """A generation that has started but not yet linked a video pad."""
    feed = SimpleNamespace(
        config=SimpleNamespace(name="cam"),
        generation=3,
        state="starting",
        video_linked=False,
        connect_source_id=77,
    )
    for key, value in over.items():
        setattr(feed, key, value)
    return feed


def _connect_runtime(feed):
    runtime = object.__new__(WallRuntime)
    runtime._stopping = False
    runtime.feeds = {"cam": feed}
    runtime.restarts = []
    runtime._request_feed_restart = (  # type: ignore[method-assign]
        lambda name, gen, reason: runtime.restarts.append((name, gen, reason))
    )
    return runtime


def test_a_generation_that_never_links_video_is_restarted() -> None:
    # The failure this exists for: after a switch was power cycled the NVR
    # half-closed the socket, rtspsrc read nothing and never hit its own
    # timeout, and the feed sat silent with no watchdog and no ladder to
    # catch it, because both live downstream of a video pad it never linked.
    feed = _connecting_feed()
    runtime = _connect_runtime(feed)
    assert runtime._give_up_on_connect("cam", 3) is False
    assert len(runtime.restarts) == 1
    assert "no video pad" in runtime.restarts[0][2]
    # The timer is spent, and must not be cancelled a second time.
    assert feed.connect_source_id is None


def test_a_feed_that_linked_video_is_left_alone() -> None:
    runtime = _connect_runtime(_connecting_feed(video_linked=True))
    runtime._give_up_on_connect("cam", 3)
    assert runtime.restarts == []


def test_a_stale_connect_deadline_is_ignored() -> None:
    # The generation it was armed for is already gone.
    feed = _connecting_feed(generation=4)
    runtime = _connect_runtime(feed)
    runtime._give_up_on_connect("cam", 3)
    assert runtime.restarts == []
    # And it must not disarm the deadline the live generation is relying on:
    # that id belongs to generation 4, not to the timer that just fired.
    assert feed.connect_source_id == 77


def test_a_feed_already_in_backoff_is_not_restarted_again() -> None:
    runtime = _connect_runtime(_connecting_feed(state="backoff"))
    runtime._give_up_on_connect("cam", 3)
    assert runtime.restarts == []


def test_a_stopping_wall_does_not_restart_a_connecting_feed() -> None:
    runtime = _connect_runtime(_connecting_feed())
    runtime._stopping = True
    runtime._give_up_on_connect("cam", 3)
    assert runtime.restarts == []



def test_a_camera_that_never_delivered_is_only_reconnected(monkeypatch) -> None:
    # The guard that matters most: a camera that is off, or behind a switch
    # that is, never reaches healthy however long it is down, so it can never
    # rebuild a branch or take the wall down. Verified on hardware twice by
    # power cycling the switch feeding three cameras.
    rebuilt: list[str] = []
    runtime = _wedging_runtime(rebuilt)
    feed = _lifetime_feed()
    for _ in range(50):
        _died(runtime, feed)
    assert rebuilt == []
    assert runtime._fatal_error is None


def test_a_feed_that_delivered_then_stalled_rebuilds_its_branch(monkeypatch) -> None:
    # camera_23rd_st in production: fifteen generations, each reaching healthy
    # and dying to the stall watchdog about five seconds later, with no
    # escalation at all under the counting version this replaced.
    rebuilt: list[str] = []
    runtime = _wedging_runtime(rebuilt)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 105.0)
    feed = _lifetime_feed()
    feed.healthy_at = 100.0
    _died(runtime, feed)
    assert rebuilt == ["cam"]
    assert feed.branches_rebuilt is True


def test_a_source_side_failure_never_rebuilds_the_branch(monkeypatch) -> None:
    # The camera stopped sending. No repair here changes that, and on a
    # rotating viewport the rebuild disturbs the feed sharing it.
    rebuilt: list[str] = []
    runtime = _wedging_runtime(rebuilt)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 105.0)
    feed = _lifetime_feed()
    feed.healthy_at = 100.0
    for _ in range(10):
        _died(runtime, feed, SOURCE, stalled=False)
    assert rebuilt == []
    assert runtime._fatal_error is None


def test_a_generation_that_ran_a_while_is_left_alone(monkeypatch) -> None:
    rebuilt: list[str] = []
    runtime = _wedging_runtime(rebuilt)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 200.0)
    feed = _lifetime_feed()
    feed.healthy_at = 100.0
    _died(runtime, feed)
    assert rebuilt == []


def test_a_stall_after_the_rebuild_restarts_the_wall(monkeypatch) -> None:
    runtime = _wedging_runtime([])
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 105.0)
    feed = _lifetime_feed()
    feed.healthy_at = 100.0
    feed.branches_rebuilt = True
    _died(runtime, feed)
    assert runtime._fatal_error is not None
    assert "outside the branch" in runtime._fatal_error


def test_the_wall_is_not_restarted_twice_for_the_same_feed(monkeypatch) -> None:
    # A camera whose encoder freezes while its TCP session stays up starves
    # the watchdog exactly like shared state does, and no number of restarts
    # fixes the camera.
    runtime = _wedging_runtime([])
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 105.0)
    feed = _lifetime_feed()
    feed.healthy_at = 100.0
    feed.branches_rebuilt = True
    feed.fatal_at = 100.0
    _died(runtime, feed)
    assert runtime._fatal_error is None


def test_the_wall_may_be_restarted_again_once_the_cooldown_passes(
    monkeypatch,
) -> None:
    runtime = _wedging_runtime([])
    later = 100.0 + WallRuntime.FATAL_COOLDOWN_SECONDS + 10.0
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: later)
    feed = _lifetime_feed()
    feed.healthy_at = later - 5.0
    feed.branches_rebuilt = True
    feed.fatal_at = 100.0
    _died(runtime, feed)
    assert runtime._fatal_error is not None


def test_the_first_frame_after_a_switch_is_timed(monkeypatch, caplog) -> None:
    # The metrics window around a switch is too short to divide by, so it
    # reports no rate at exactly the moment of interest. This is the only
    # measure of how long a tile takes to actually change.
    runtime = object.__new__(WallRuntime)
    runtime.Gst = SimpleNamespace(PadProbeReturn=SimpleNamespace(OK="OK"))
    viewport = SimpleNamespace(
        # kms-planes: its own sink, no compositor pad.
        mixer_pad=None,
        config=SimpleNamespace(index=9),
        active_feed="coop",
        queued_frames=0,
        output_total=0,
        last_output_at=None,
        switched_at=100.0,
    )
    runtime.viewports = {"v9": viewport}
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 101.5)
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        runtime._on_viewport_buffer(None, None, "v9")
    assert "1500ms after the switch" in caplog.text
    # Reported once per switch, not once per frame.
    assert viewport.switched_at is None
    with caplog.at_level("INFO", logger="viewwall.gst_runtime"):
        caplog.clear()
        runtime._on_viewport_buffer(None, None, "v9")
    assert caplog.text == ""


def _tile_runtime(last_output_at, feed_state="healthy"):
    """A wall with one viewport whose branch rebuild records the call."""
    runtime = object.__new__(WallRuntime)
    runtime._stopping = False
    runtime._fatal_error = None
    runtime.rebuilt: list[str] = []
    viewport = SimpleNamespace(
        # kms-planes: its own sink, no compositor pad.
        mixer_pad=None,
        config=SimpleNamespace(index=1),
        active_feed="cam",
        last_output_at=last_output_at,
        output_total=0,
        rate_sample=None,
        keeping_up_at=None,
        rate_shown=0,
        rate_decoded=0,
    )
    runtime.viewports = {"v1": viewport}
    runtime.feeds = {"cam": SimpleNamespace(state=feed_state, decoded_total=0)}
    runtime._rebuild_one_branch_safely = (  # type: ignore[method-assign]
        lambda vp, name: runtime.rebuilt.append(name)
    )
    return runtime, viewport


def _rate_runtime(shown_per_tick: int, decoded_per_tick: int):
    """A wall whose tile receives `shown` of the `decoded` frames each tick."""
    runtime, viewport = _tile_runtime(last_output_at=None)
    feed = runtime.feeds["cam"]
    step = {"n": 0}

    def tick(now: float) -> bool:
        step["n"] += 1
        viewport.output_total = shown_per_tick * step["n"]
        feed.decoded_total = decoded_per_tick * step["n"]
        return runtime._tile_falling_behind(viewport, feed, now)

    return runtime, viewport, tick


def test_a_tile_keeping_up_is_not_repaired() -> None:
    # Healthy is parity: every static viewport on the wall reports the queued
    # and decoded rates equal to the first decimal.
    _runtime, _vp, tick = _rate_runtime(shown_per_tick=30, decoded_per_tick=30)
    assert [tick(t) for t in (0.0, 5.0, 40.0, 80.0)] == [False] * 4


def test_a_tile_showing_a_trickle_is_repaired() -> None:
    # The production fault: a tile rendering about one frame every three
    # seconds from a feed decoding thirty, for two hours, while the stall
    # watchdog and the quiet test both reported healthy.
    _runtime, viewport, tick = _rate_runtime(shown_per_tick=1, decoded_per_tick=30)
    assert tick(0.0) is False
    assert tick(5.0) is False
    assert tick(5.0 + WallRuntime.TILE_SLOW_SECONDS) is True
    assert viewport.rate_shown == 1
    assert viewport.rate_decoded == 30


def test_a_brief_dip_is_not_enough() -> None:
    # A rotation, a backoff or a reconnect can starve a tile for a moment.
    _runtime, _vp, tick = _rate_runtime(shown_per_tick=1, decoded_per_tick=30)
    tick(0.0)
    assert tick(5.0) is False
    assert tick(5.0 + WallRuntime.TILE_SLOW_SECONDS / 2) is False


def test_a_tile_recovering_before_the_dwell_starts_over() -> None:
    runtime, viewport, _tick = _rate_runtime(1, 30)
    feed = runtime.feeds["cam"]
    runtime._tile_falling_behind(viewport, feed, 0.0)
    viewport.output_total, feed.decoded_total = 1, 30
    assert runtime._tile_falling_behind(viewport, feed, 5.0) is False
    # Parity returns, so the clock restarts and the later tick cannot fire.
    viewport.output_total, feed.decoded_total = 31, 60
    assert runtime._tile_falling_behind(viewport, feed, 10.0) is False
    viewport.output_total, feed.decoded_total = 32, 90
    assert runtime._tile_falling_behind(
        viewport, feed, 10.0 + WallRuntime.TILE_SLOW_SECONDS / 2
    ) is False


def test_a_collapse_still_matures_across_rotations() -> None:
    # The viewport this fault was seen on rotates every eight seconds while
    # the poll runs every five, so the dwell has to survive a switch. It also
    # has to survive the baseline being dropped at that switch, which is
    # necessary because the two feeds' decoded totals are unrelated.
    runtime, viewport, _tick = _rate_runtime(1, 30)
    coop, run = SimpleNamespace(decoded_total=1_000_000), SimpleNamespace(
        decoded_total=800_000
    )
    now = 0.0
    fired = False
    for step in range(12):
        if step % 2 == 0:
            # A rotation: the wall drops the stale baseline, keeps the dwell.
            viewport.rate_sample = None
        feed = coop if step % 4 < 2 else run
        viewport.output_total += 1
        feed.decoded_total += 30
        fired = runtime._tile_falling_behind(viewport, feed, now)
        if fired:
            break
        now += 5.0
    assert fired, "a sustained collapse must mature despite rotations"
    assert now >= WallRuntime.TILE_SLOW_SECONDS


def test_a_rotation_does_not_reset_the_dwell() -> None:
    # The specific defect: without dropping the baseline, a switch produced a
    # negative decoded delta, which passes the floor test and restarts the
    # clock. With it dropped, the dwell keeps running.
    runtime, viewport, _tick = _rate_runtime(1, 30)
    feed = runtime.feeds["cam"]
    viewport.output_total, feed.decoded_total = 1, 30
    runtime._tile_falling_behind(viewport, feed, 0.0)
    viewport.output_total, feed.decoded_total = 2, 60
    runtime._tile_falling_behind(viewport, feed, 5.0)
    started = viewport.keeping_up_at
    # Rotate: baseline dropped, dwell untouched.
    viewport.rate_sample = None
    viewport.output_total, feed.decoded_total = 3, 90
    runtime._tile_falling_behind(viewport, feed, 10.0)
    assert viewport.keeping_up_at == started


def test_a_feed_decoding_nothing_never_matures_into_a_rebuild() -> None:
    # A camera that is merely off must not have its branch rebuilt: with
    # nothing decoded the comparison reads 0 >= 0, resetting the dwell on
    # every tick however long the outage lasts.
    runtime, viewport, _tick = _rate_runtime(0, 0)
    feed = runtime.feeds["cam"]
    viewport.output_total, feed.decoded_total = 1, 30
    runtime._tile_falling_behind(viewport, feed, 0.0)
    for t in (5.0, 10.0, 10.0 + WallRuntime.TILE_SLOW_SECONDS * 3):
        viewport.output_total, feed.decoded_total = 1, 30
        assert runtime._tile_falling_behind(viewport, feed, t) is False



def test_a_tile_that_goes_quiet_under_a_healthy_feed_is_repaired(monkeypatch) -> None:
    # The failure no other signal can see: the watchdog and the healthy line
    # both sit inside the feed bin, so a branch that stops delivering leaves
    # the feed reporting healthy and the tile black with nothing logged.
    runtime, _ = _tile_runtime(last_output_at=100.0)
    monkeypatch.setattr(
        "viewwall.gst_runtime.time.monotonic",
        lambda: 100.0 + WallRuntime.TILE_QUIET_SECONDS + 1.0,
    )
    assert runtime._poll_viewport_output() is True
    assert runtime.rebuilt == ["cam"]


def test_a_tile_still_delivering_is_left_alone(monkeypatch) -> None:
    runtime, _ = _tile_runtime(last_output_at=100.0)
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: 105.0)
    runtime._poll_viewport_output()
    assert runtime.rebuilt == []


def test_a_feed_already_being_replaced_is_left_to_the_watchdog(monkeypatch) -> None:
    # Otherwise a feed-side stall trips both this and the watchdog, and the
    # branch is rebuilt under a feed bin that is already being replaced.
    runtime, _ = _tile_runtime(last_output_at=100.0, feed_state="backoff")
    monkeypatch.setattr(
        "viewwall.gst_runtime.time.monotonic",
        lambda: 100.0 + WallRuntime.TILE_QUIET_SECONDS + 1.0,
    )
    runtime._poll_viewport_output()
    assert runtime.rebuilt == []


def test_the_quiet_window_outlasts_the_stall_watchdog() -> None:
    # The ordering that keeps the two from racing.
    assert (
        WallRuntime.TILE_QUIET_SECONDS * 1000 > WallRuntime.FEED_STALL_TIMEOUT_MS
    )


def test_a_repaired_tile_is_given_a_fresh_window(monkeypatch) -> None:
    # Without this the next poll would see the same stale timestamp and
    # rebuild again every five seconds.
    runtime, viewport = _tile_runtime(last_output_at=100.0)
    now = 100.0 + WallRuntime.TILE_QUIET_SECONDS + 1.0
    monkeypatch.setattr("viewwall.gst_runtime.time.monotonic", lambda: now)
    runtime._poll_viewport_output()
    assert viewport.last_output_at == now
    runtime._poll_viewport_output()
    assert runtime.rebuilt == ["cam"]


def _wedging_runtime(rebuilt: list[str] | None = None) -> WallRuntime:
    """A runtime whose branch rebuild records the call instead of doing it."""
    runtime = object.__new__(WallRuntime)
    runtime.viewports = {}
    runtime._fatal_error = None
    runtime.config = SimpleNamespace(viewports=[])
    runtime.stop = lambda: None  # type: ignore[method-assign]
    if rebuilt is not None:
        runtime._rebuild_feed_branches = lambda feed: (  # type: ignore[method-assign]
            rebuilt.append(feed.config.name) or True
        )
    return runtime



def _branch_viewport(index: int, feeds: tuple[str, ...], active: str | None):
    return SimpleNamespace(
        config=SimpleNamespace(index=index, feeds=feeds, name=f"viewport{index}"),
        active_feed=active,
    )


def test_a_rotating_viewport_is_rebuilt_too() -> None:
    # cam_a is on screen while cam_b is wedged. Skipping the repair would
    # leave cam_b wedged forever and the viewport silently showing one camera
    # where two were configured, which is harder to spot than a black tile.
    # The pad released is the inactive one, verified against real GStreamer
    # to leave the active branch streaming.
    runtime = object.__new__(WallRuntime)
    runtime.viewports = {
        "v9": _branch_viewport(9, ("cam_a", "cam_b"), "cam_a")
    }
    done: list[tuple[int, str]] = []
    runtime._rebuild_one_branch = lambda vp, name: done.append(  # type: ignore[method-assign]
        (vp.config.index, name)
    )
    feed = SimpleNamespace(config=SimpleNamespace(name="cam_b"))
    assert runtime._rebuild_feed_branches(feed) is True
    assert done == [(9, "cam_b")]


def test_only_viewports_carrying_the_feed_are_rebuilt() -> None:
    runtime = object.__new__(WallRuntime)
    runtime.viewports = {
        "v6": _branch_viewport(6, ("cam",), None),
        "v9": _branch_viewport(9, ("cam_a", "cam_b"), "cam_a"),
    }
    done: list[tuple[int, str]] = []
    runtime._rebuild_one_branch = lambda vp, name: done.append(  # type: ignore[method-assign]
        (vp.config.index, name)
    )
    feed = SimpleNamespace(config=SimpleNamespace(name="cam"))
    assert runtime._rebuild_feed_branches(feed) is True
    assert done == [(6, "cam")]


def test_a_dark_viewport_is_rebuilt() -> None:
    # Nothing is on screen, so the blast radius is zero.
    runtime = object.__new__(WallRuntime)
    runtime.viewports = {"v6": _branch_viewport(6, ("cam",), None)}
    done: list[tuple[int, str]] = []
    runtime._rebuild_one_branch = lambda vp, name: done.append(  # type: ignore[method-assign]
        (vp.config.index, name)
    )
    feed = SimpleNamespace(config=SimpleNamespace(name="cam"))
    assert runtime._rebuild_feed_branches(feed) is True
    assert done == [(6, "cam")]


def test_a_failed_rebuild_is_fatal() -> None:
    # The graph is left without a branch and nothing else can restore it.
    runtime = object.__new__(WallRuntime)
    runtime.viewports = {"v6": _branch_viewport(6, ("cam",), None)}
    fatal: list[str] = []
    runtime._fatal = fatal.append  # type: ignore[method-assign]

    def boom(_vp, _name):
        raise RuntimeDependencyError("could not remove queue")

    runtime._rebuild_one_branch = boom  # type: ignore[method-assign]
    feed = SimpleNamespace(config=SimpleNamespace(name="cam"))
    assert runtime._rebuild_feed_branches(feed) is False
    assert fatal and "branch rebuild failed" in fatal[0]


def test_the_branch_rebuild_runs_after_the_feed_bin_is_torn_down() -> None:
    # Ordering matters and nothing downstream can detect it: rebuilding a
    # branch releases the tee pad feeding it, and a source still running into
    # a tee with no branches left dies instantly with "not-linked". Verified
    # against real GStreamer, where the wrong order fails every time.
    import inspect

    body = inspect.getsource(WallRuntime._restart_feed)
    assert body.index("_teardown_feed_attempt") < body.index(
        "_note_generation_lifetime"
    )
