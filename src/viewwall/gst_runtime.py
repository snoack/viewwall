from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
import logging
import os
import random
import signal
import socket
import time
from typing import Any

from .config import (
    TLS_NO_VERIFY_FLAGS,
    TRANSPORTS,
    TLS_VERIFY_FLAGS,
    AppConfig,
    FeedConfig,
    ViewportConfig,
)
from .display import DisplayState, current_modes, detect_displays
from .journal import format_fields
from .layout import ResolvedViewport, SourceCrop, resolve_layout


LOG = logging.getLogger(__name__)


def _offers_srtp(structure: Any) -> bool:
    """True when an RTP caps structure describes SRTP-encrypted media.

    RFC 4568 puts the key in an SDP "a=crypto" line, which rtspsrc surfaces as
    an "a-crypto" caps field; the SAVP profile names it directly. Neither is
    vendor-specific.
    """
    if structure.has_field("a-crypto"):
        return True
    profile = (structure.get_string("profile") or "").upper()
    return "SAVP" in profile


class RuntimeDependencyError(RuntimeError):
    """Raised when a required native runtime component is unavailable."""


def _load_gstreamer() -> tuple[Any, Any, Any]:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import GLib, Gst, GstVideo
    except (ImportError, ValueError) as exc:
        raise RuntimeDependencyError(
            "GStreamer introspection is unavailable; install python3-gi, "
            "gir1.2-gstreamer-1.0, and gir1.2-gst-plugins-base-1.0"
        ) from exc
    return GLib, Gst, GstVideo


@dataclass
class FeedRuntime:
    config: FeedConfig
    decode_queue: Any
    tee: Any
    bin: Any | None = None
    source: Any | None = None
    audio_queue: Any | None = None
    watchdog: Any | None = None
    depay: Any | None = None
    parser: Any | None = None
    decoder: Any | None = None
    codec: str | None = None
    source_size: tuple[int, int] | None = None
    last_frame_at: float | None = None
    max_frame_gap: float | None = None
    decoded_frames: int = 0
    healthy_at: float | None = None
    short_lived_generations: int = 0
    branches_rebuilt: bool = False
    rebuilt_at_generation: int | None = None
    observed_fps_applied: bool = False
    caps_fps_known: bool = False
    watchdog_reported: bool = False
    video_linked: bool = False
    audio_linked: bool = False
    generation: int = 0
    state: str = "stopped"
    failures: int = 0
    retry_source_id: int | None = None
    stable_source_id: int | None = None


@dataclass
class ViewportRuntime:
    config: ViewportConfig
    display_name: str
    selector: Any
    aspect: Any
    valve: Any
    output_queue: Any
    sink: Any
    plane_id: int
    sink_generation: int = 0
    selector_pads: dict[str, Any] = field(default_factory=dict)
    branch_crops: dict[str, Any] = field(default_factory=dict)
    branch_queues: dict[str, Any] = field(default_factory=dict)
    branch_tee_pads: dict[str, Any] = field(default_factory=dict)
    branch_generation: int = 0
    active_index: int = 0
    active_feed: str | None = None
    resolved: ResolvedViewport | None = None
    crop_values: SourceCrop | None = None
    pixel_aspect_ratio: Fraction | None = None
    queued_frames: int = 0
    metrics_since: float | None = None
    metrics_rotated: bool = False
    # Cumulative sink counters, not per-interval: kmssink reports totals, so
    # the interval rate is the difference against what was read last time.
    presented_total: int = 0
    dropped_total: int = 0


class WallRuntime:
    """Build and control a zero-copy GStreamer/KMS camera wall."""

    # A stalled feed is one that has missed this many frames at its own rate,
    # rather than one that has been quiet for a fixed number of seconds.
    # Emitted by GstBaseSink, not by kmssink, and not silenced by qos=false:
    # that stops the QoS events, not the late-buffer counting behind them.
    _LATE_BUFFER_WARNING = "A lot of buffers are being dropped"

    # Below this a window is too short for the frame count to imply a rate.
    MIN_METRICS_WINDOW_S = 1.0

    # A generation that reached healthy and died inside this many seconds
    # delivered almost nothing: the feed connected but no video followed.
    SHORT_GENERATION_SECONDS = 15.0
    # Consecutive such generations before saying so. One is a coincidence --
    # a camera can drop just after connecting -- and a run of them is not.
    SHORT_GENERATION_LIMIT = 3

    STALL_FRAMES = 45
    MIN_STALL_TIMEOUT_MS = 5_000
    FEED_STALL_TIMEOUT_MS = 15_000
    FEED_STABLE_SECONDS = 60
    RETRY_DELAYS_SECONDS = (1, 2, 5, 10, 30)

    def __init__(
        self,
        config: AppConfig,
        displays: Mapping[str, DisplayState] | None = None,
    ) -> None:
        self.config = config
        demand = {
            display.name: len(config.viewports_for(display))
            for display in config.displays
        }
        for display in config.displays:
            if demand[display.name] == 0:
                raise RuntimeDependencyError(
                    f"display {display.name} has no viewports; remove it or give "
                    "it one"
                )
        self.displays = (
            dict(displays)
            if displays is not None
            else detect_displays(config.displays, demand)
        )
        for display in config.displays:
            state = self.displays[display.name]
            if len(state.plane_ids) < demand[display.name]:
                raise RuntimeDependencyError(
                    f"display {display.name}: need {demand[display.name]} KMS "
                    f"overlay planes, found {len(state.plane_ids)}; use fewer "
                    "viewports, or rotate several feeds through one viewport"
                )

        self.GLib, self.Gst, self.GstVideo = _load_gstreamer()
        self.Gst.init(None)
        self.pipeline = self.Gst.Pipeline.new("viewwall")
        if self.pipeline is None:
            raise RuntimeDependencyError("could not create GStreamer pipeline")
        self.loop = self.GLib.MainLoop()
        self.drm_fd = os.open(config.drm.device, os.O_RDWR | os.O_CLOEXEC)
        self.feeds: dict[str, FeedRuntime] = {}
        self.viewports: dict[str, ViewportRuntime] = {}
        self.background_sinks: dict[str, Any] = {}
        self._feed_bins: dict[str, tuple[str, int]] = {}
        self._retired_feed_bins: dict[str, Any] = {}
        self._stopping = False
        self._metrics_sampled_at = 0.0
        self._wall_dark = False
        self._fatal_error: str | None = None
        try:
            self._build()
        except Exception:
            # _build() raises on any missing element or unusable plane. The
            # process usually exits straight after, but close() is what owns
            # the DRM fd, so let it run rather than relying on that.
            self.close()
            raise

    @property
    def display(self) -> DisplayState:
        """The sole display's state.

        Several displays have no single answer, so this raises rather than
        picking one; the multi-display paths take a name.
        """
        if len(self.displays) != 1:
            raise RuntimeDependencyError(
                "this wall drives several displays; address one by name"
            )
        return next(iter(self.displays.values()))

    @display.setter
    def display(self, state: DisplayState) -> None:
        if len(self.displays) != 1:
            raise RuntimeDependencyError(
                "this wall drives several displays; address one by name"
            )
        self.displays[next(iter(self.displays))] = state

    def _element(self, factory: str, name: str) -> Any:
        element = self.Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeDependencyError(f"required GStreamer element is unavailable: {factory}")
        return element

    def _add(self, *elements: Any) -> None:
        for element in elements:
            self.pipeline.add(element)

    @staticmethod
    def _link_many(*elements: Any) -> None:
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeDependencyError(
                    f"could not link {left.get_name()} to {right.get_name()}"
                )

    @staticmethod
    def _set_if_present(element: Any, prop: str, value: object) -> None:
        if element.find_property(prop) is not None:
            element.set_property(prop, value)

    def _set_object_arg_if_present(self, element: Any, prop: str, value: str) -> None:
        if element.find_property(prop) is not None:
            self.Gst.util_set_object_arg(element, prop, value)

    def _new_kms_sink(
        self, safe_name: str, plane_id: int, connector_id: int, generation: int
    ) -> Any:
        sink = self._element("kmssink", f"kms_{safe_name}_{generation}")
        sink.set_property("fd", os.dup(self.drm_fd))
        sink.set_property("plane-id", plane_id)
        sink.set_property("connector-id", connector_id)
        self._set_if_present(sink, "force-aspect-ratio", False)
        self._set_if_present(sink, "skip-vsync", True)
        # An offline viewport's valve intentionally supplies no preroll buffer.
        # Do not let such a viewport hold the complete pipeline in ASYNC_PENDING.
        sink.set_property("async", False)
        sink.set_property("sync", True)
        # QoS off. kmssink presents at most one buffer per vblank, so with nine
        # planes on a 60Hz output it reports "a lot of buffers are being
        # dropped" and sends QoS events upstream. v4l2h264dec obeys them and
        # discards frames *before decoding*, which starved feeds to as little
        # as 15% of source rate (one feed sat at 4 fps from 30 even when it was
        # the only feed running). Late frames are already handled by the leaky
        # queues, so the wall drops them at the display rather than at the
        # decoder. Measured on a Pi 3 with nine feeds: 65% of source rate with
        # QoS on, 99% with it off.
        self._set_if_present(sink, "qos", False)
        return sink

    def _build(self) -> None:
        for factory in ("rtspsrc", "watchdog"):
            if self.Gst.ElementFactory.find(factory) is None:
                raise RuntimeDependencyError(
                    f"required GStreamer element is unavailable: {factory}"
                )
        self._build_background()
        self._build_viewports()
        self._build_feeds()
        self._connect_feed_branches()
        self._select_initial_feeds()
        self._apply_layout()

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _build_background(self) -> None:
        """Paint under the viewports so the console does not show through.

        Only the viewport rectangles are ever drawn. Below them the primary
        plane still holds whatever the framebuffer console left there, and it
        shows in the gaps, in the outer margin, and across any viewport whose
        plane has been disabled because every feed behind it is down.

        This is a modeset, not another overlay plane, and the distinction is
        the whole reason it is affordable. As an overlay a full-screen
        background costs about six 640x360 viewports of VC4 HVS budget --
        measured on a Pi 3: it fits alongside two viewports and fails with
        ENOSPC at three. The cost follows plane *width* across every
        scanline, so neither NV12 nor a smaller buffer scaled up avoids it.
        force-modesetting hands the buffer straight to the CRTC instead,
        which never enters plane compositing: nine viewports plus a
        full-screen background then run at unchanged framerates.

        force-modesetting is load-bearing for a second reason. Without it
        kmssink picks the first free *overlay* rather than erroring, quietly
        taking a plane a viewport needs.

        Never fatal. A wall with a visible console is what every release so
        far has shipped, and it beats no wall at all.
        """
        colour = self.config.drm.background
        if colour is None:
            return
        for display_name, state in self.displays.items():
            safe = display_name.replace("-", "_")
            try:
                source = self._element("videotestsrc", f"background_src_{safe}")
                filt = self._element("capsfilter", f"background_caps_{safe}")
                sink = self._element("kmssink", f"kms_background_{safe}")
            except RuntimeDependencyError as exc:
                LOG.warning(
                    "display %s: background unavailable: %s", display_name, exc
                )
                continue
            sink.set_property("fd", os.dup(self.drm_fd))
            sink.set_property("connector-id", state.connector_id)
            self._set_if_present(sink, "force-modesetting", True)
            # The CRTC goes back to the console's framebuffer when the DRM fd
            # closes, so the console returns on exit either way, including
            # after a kill -9. This only stops kmssink restoring the mode
            # itself, which it has no reason to do here.
            self._set_if_present(sink, "restore-crtc", False)
            self._set_if_present(sink, "skip-vsync", True)
            self._set_if_present(sink, "qos", False)
            # One still frame, so there is nothing to synchronise against and
            # no reason to hold the pipeline in ASYNC_PENDING waiting for it.
            sink.set_property("async", False)
            sink.set_property("sync", False)
            source.set_property("pattern", "solid-color")
            # A single buffer would not hold: the CRTC reverts as soon as the
            # element leaves PLAYING. The source stays live at a low rate
            # instead, which costs nothing once the frame is on screen.
            source.set_property("is-live", True)
            # videotestsrc takes 0xAARRGGBB and reads a zero alpha byte as
            # fully transparent, so the colour has to carry one.
            source.set_property("foreground-color", 0xFF000000 | int(colour[1:], 16))
            filt.set_property(
                "caps",
                self.Gst.Caps.from_string(
                    f"video/x-raw,width={state.width},"
                    f"height={state.height},framerate=1/2"
                ),
            )
            self._add(source, filt, sink)
            self._link_many(source, filt, sink)
            self.background_sinks[display_name] = sink
            LOG.info("display %s: background %s", display_name, colour)

    def _build_viewports(self) -> None:
        next_plane = {name: 0 for name in self.displays}
        for viewport_config in self.config.viewports:
            safe = viewport_config.name.replace("-", "_")
            selector = self._element("input-selector", f"selector_{safe}")
            # There is deliberately no videocrop in this chain. Cropping is
            # what keeps a plane on the cheap 1:1 path -- nine *scaling* planes
            # exhaust the Pi 3's KMS resources, nine 1:1 planes do not -- but a
            # crop placed here, after the selector, is shared by every branch
            # and has to renegotiate whenever the active feed changes. That
            # renegotiation broke rotating viewports: kmssink sizes its buffer
            # pool from the cropped caps, so a switch left the pool disagreeing
            # with the decoder's concrete GstVideoMeta. Imports failed with
            # "gst_video_frame_map_id: assertion info->finfo->format ==
            # meta->format", kmssink fell back to copy_to_dumb_buffer, and the
            # RTSP source died with "internal data stream error".
            #
            # Each branch gets its own videocrop upstream instead, in
            # _connect_feed_branches(), where it faces exactly one decoder for
            # the lifetime of the branch and its caps never change.
            aspect = self._element("capssetter", f"aspect_{safe}")
            queue = self._element("queue", f"output_queue_{safe}")
            valve = self._element("valve", f"valve_{safe}")
            display_name = viewport_config.display
            state = self.displays[display_name]
            plane_id = state.plane_ids[next_plane[display_name]]
            next_plane[display_name] += 1
            sink = self._new_kms_sink(safe, plane_id, state.connector_id, 0)

            self._set_if_present(selector, "sync-streams", False)
            self._set_if_present(selector, "cache-buffers", True)
            self._set_if_present(selector, "drop-backwards", True)
            # Measured on a Pi 3: a 2-buffer output queue starves kmssink and
            # holds the wall to ~10 fps/viewport; 32 raises it to ~17 fps/viewport.
            # Deeper than this does not help and only adds latency.
            #
            # Retested at 8 after the QoS fix, in case the depth was
            # compensating for something already solved: kmssink immediately
            # resumed logging "a lot of buffers are being dropped" once a
            # second, which is the condition that made it send the QoS events
            # in the first place. 32 is still needed.
            queue.set_property("max-size-buffers", 32)
            queue.set_property("max-size-bytes", 0)
            queue.set_property("max-size-time", 0)
            self.Gst.util_set_object_arg(queue, "leaky", "downstream")
            valve.set_property("drop", True)
            self._set_object_arg_if_present(
                valve, "drop-mode", "forward-sticky-events"
            )
            # On the output queue's source pad, not the sink's sink pad: this
            # still counts what leaves the leaky queue for the plane, but the
            # queue outlives a sink. _show_viewport_offline() replaces the sink on
            # every outage, and a probe attached to the old one was silently
            # lost, so after any recovery a viewport reported 0 fps for the rest of
            # the process while displaying video perfectly well.
            queue_src = queue.get_static_pad("src")
            if queue_src is not None:
                queue_src.add_probe(
                    self.Gst.PadProbeType.BUFFER,
                    self._on_viewport_buffer,
                    viewport_config.name,
                )

            chain = (selector, valve, aspect, queue, sink)
            self._add(*chain)
            self._link_many(*chain)

            runtime = ViewportRuntime(
                config=viewport_config,
                display_name=display_name,
                selector=selector,
                aspect=aspect,
                valve=valve,
                output_queue=queue,
                sink=sink,
                plane_id=plane_id,
            )
            self.viewports[viewport_config.name] = runtime

    def _build_feeds(self) -> None:
        used_feeds = {
            feed_name
            for viewport in self.config.viewports
            for feed_name in viewport.feeds
        }
        for feed_config in self.config.feeds.values():
            if feed_config.name not in used_feeds:
                LOG.info("feed %s is not assigned to a viewport; leaving it stopped", feed_config.name)
                continue
            safe = feed_config.name.replace("-", "_")
            decode_queue = self._element("queue", f"decode_queue_{safe}")
            tee = self._element("tee", f"tee_{safe}")

            # Measured on a Pi 3 with nine feeds: 3 buffers here starves the
            # slower feeds (67% of wire on average, worst feed 16%); 8 lifts
            # that to 76% average and 40% worst. Deeper is worse again (16
            # gives 70%, 32 gives 65%), so this is a plateau, not a guess.
            decode_queue.set_property("max-size-buffers", 8)
            decode_queue.set_property("max-size-bytes", 0)
            decode_queue.set_property("max-size-time", 0)
            self.Gst.util_set_object_arg(decode_queue, "leaky", "downstream")

            self._add(decode_queue, tee)
            self._link_many(decode_queue, tee)

            runtime = FeedRuntime(
                config=feed_config,
                decode_queue=decode_queue,
                tee=tee,
            )
            self.feeds[feed_config.name] = runtime

    @classmethod
    def retry_delay_seconds(cls, failures: int, jitter: float = 1.0) -> float:
        index = min(max(failures, 1) - 1, len(cls.RETRY_DELAYS_SECONDS) - 1)
        return max(0.1, cls.RETRY_DELAYS_SECONDS[index] * jitter)

    def _start_feed_attempt(self, feed: FeedRuntime) -> None:
        feed.generation += 1
        generation = feed.generation
        safe = feed.config.name.replace("-", "_")
        feed_bin = self.Gst.Bin.new(f"feed_{safe}_{generation}")
        if feed_bin is None:
            raise RuntimeDependencyError(f"could not create bin for feed {feed.config.name}")

        source = self._element("rtspsrc", f"rtsp_{safe}")
        audio_queue = self._element("queue", f"audio_queue_{safe}")
        audio_sink = self._element("fakesink", f"audio_sink_{safe}")
        watchdog = self._element("watchdog", f"watchdog_{safe}")

        source.set_property("location", feed.config.uri)
        source.set_property("latency", feed.config.latency_ms)
        self._set_if_present(source, "drop-on-latency", True)
        self._set_if_present(source, "do-rtsp-keep-alive", True)
        self._set_if_present(source, "tcp-timeout", 20_000_000)
        if feed.config.uri.lower().startswith("rtsps://"):
            # A UniFi Protect NVR presents a self-signed certificate, so the
            # default validation rejects it and the stream never starts.
            flags = TLS_VERIFY_FLAGS if feed.config.verify_tls else TLS_NO_VERIFY_FLAGS
            self._set_if_present(source, "tls-validation-flags", flags)
            if not feed.config.verify_tls:
                LOG.warning(
                    "feed %s: RTSPS certificate validation disabled; the "
                    "transport is encrypted but the server is not verified",
                    feed.config.name,
                )
        # A flags property, so "auto" is a real value: it offers every lower
        # transport and lets the server pick, which is rtspsrc's own default.
        self.Gst.util_set_object_arg(
            source, "protocols", TRANSPORTS[feed.config.transport]
        )

        audio_queue.set_property("max-size-buffers", 4)
        audio_queue.set_property("max-size-bytes", 0)
        audio_queue.set_property("max-size-time", 0)
        self.Gst.util_set_object_arg(audio_queue, "leaky", "downstream")
        audio_sink.set_property("sync", False)
        audio_sink.set_property("async", False)
        # A starting value only. Once the feed negotiates caps its real
        # framerate is known, and _apply_feed_watchdog() scales this to suit it:
        # a fixed timeout that is right for 30fps is far too tight for a 3fps
        # camera, which can legitimately go seconds between frames.
        watchdog.set_property("timeout", self.FEED_STALL_TIMEOUT_MS)

        for element in (source, audio_queue, audio_sink, watchdog):
            feed_bin.add(element)
        self._link_many(audio_queue, audio_sink)

        ghost = self.Gst.GhostPad.new("video", watchdog.get_static_pad("src"))
        if ghost is None or not feed_bin.add_pad(ghost):
            raise RuntimeDependencyError(
                f"could not create output pad for feed {feed.config.name}"
            )
        ghost.add_probe(
            self.Gst.PadProbeType.EVENT_DOWNSTREAM,
            self._on_feed_event,
            (feed.config.name, generation),
        )
        ghost.add_probe(
            self.Gst.PadProbeType.BUFFER,
            self._on_feed_first_buffer,
            (feed.config.name, generation),
        )
        # Separate from the first-buffer probe, which removes itself: this one
        # stays to learn the frame interval of feeds whose caps omit a rate.
        ghost.add_probe(
            self.Gst.PadProbeType.BUFFER,
            self._on_feed_buffer,
            (feed.config.name, generation),
        )

        feed.bin = feed_bin
        feed.source = source
        feed.audio_queue = audio_queue
        feed.watchdog = watchdog
        feed.depay = None
        feed.parser = None
        feed.decoder = None
        feed.codec = None
        feed.source_size = None
        feed.last_frame_at = None
        feed.max_frame_gap = None
        feed.healthy_at = None
        feed.observed_fps_applied = False
        feed.caps_fps_known = False
        feed.watchdog_reported = False
        feed.decoded_frames = 0
        feed.video_linked = False
        feed.audio_linked = False
        feed.state = "starting"
        self._feed_bins[feed_bin.get_name()] = (feed.config.name, generation)

        self.pipeline.add(feed_bin)
        if not feed_bin.link(feed.decode_queue):
            raise RuntimeDependencyError(
                f"could not connect restartable bin for feed {feed.config.name}"
            )
        source.connect("pad-added", self._on_rtsp_pad, feed.config.name, generation)
        self._prime_offline_viewports_for_feed(feed.config.name)
        if not feed_bin.sync_state_with_parent():
            raise RuntimeDependencyError(f"could not start feed {feed.config.name}")
        LOG.info("feed %s: starting generation %d", feed.config.name, generation)

    def _teardown_feed_attempt(self, feed: FeedRuntime) -> None:
        feed_bin = feed.bin
        if feed.stable_source_id is not None:
            self.GLib.source_remove(feed.stable_source_id)
            feed.stable_source_id = None
        if feed_bin is not None:
            bin_name = feed_bin.get_name()
            feed_bin.set_locked_state(True)
            state_result = feed_bin.set_state(self.Gst.State.NULL)
            if state_result == self.Gst.StateChangeReturn.FAILURE:
                raise RuntimeDependencyError(
                    f"could not stop failed bin for feed {feed.config.name}"
                )
            feed_bin.unlink(feed.decode_queue)
            if not self.pipeline.remove(feed_bin):
                raise RuntimeDependencyError(
                    f"could not remove failed bin for feed {feed.config.name}"
                )
            # Keep the retired bin alive briefly so late bus messages retain
            # their ancestry and cannot be mistaken for global pipeline errors.
            self._retired_feed_bins[bin_name] = feed_bin
            self.GLib.timeout_add_seconds(30, self._forget_feed_bin, bin_name)
        feed.bin = None
        feed.source = None
        feed.audio_queue = None
        feed.watchdog = None
        feed.depay = None
        feed.parser = None
        feed.decoder = None
        feed.codec = None
        feed.source_size = None
        feed.video_linked = False
        feed.audio_linked = False

    def _forget_feed_bin(self, bin_name: str) -> bool:
        self._feed_bins.pop(bin_name, None)
        self._retired_feed_bins.pop(bin_name, None)
        return False

    def _schedule_feed_retry(self, feed: FeedRuntime) -> None:
        jitter = random.uniform(0.85, 1.15)
        delay = self.retry_delay_seconds(feed.failures, jitter)
        generation = feed.generation
        feed.retry_source_id = self.GLib.timeout_add(
            max(1, round(delay * 1000)),
            self._retry_feed,
            feed.config.name,
            generation,
        )
        LOG.info("feed %s: retrying in %.1f seconds", feed.config.name, delay)

    def _request_feed_restart(self, feed_name: str, generation: int, reason: str) -> None:
        self.GLib.idle_add(self._restart_feed, feed_name, generation, reason)

    def _restart_feed(self, feed_name: str, generation: int, reason: str) -> bool:
        if self._stopping:
            return False
        feed = self.feeds[feed_name]
        if feed.generation != generation or feed.state in ("backoff", "unsupported"):
            return False
        LOG.warning("feed %s: restarting after %s", feed_name, reason)
        feed.state = "backoff"
        feed.failures += 1
        try:
            self._teardown_feed_attempt(feed)
        except RuntimeDependencyError as exc:
            self._fatal(str(exc))
            return False
        self._select_alternate_for_failed_feed(feed_name)
        # After the teardown, never before: rebuilding a branch releases the
        # tee pad it is fed from, and a source still running into a tee with
        # no branches left errors out with "not-linked" on the spot.
        self._note_generation_lifetime(feed)
        self._schedule_feed_retry(feed)
        return False

    def _note_generation_lifetime(self, feed: FeedRuntime) -> None:
        """Say so when a feed keeps dying immediately after connecting.

        A feed is called healthy on its first buffer, so one frame followed by
        silence reads in the log exactly like a feed that ran well and then
        broke -- the same "video is healthy" line, once per generation. Told
        apart only by noticing that the healthy lines repeat every few seconds,
        which is not something the log says anywhere.

        The two have different causes and different fixes. A feed that runs and
        later fails is the camera or the network, and retrying is the answer. A
        feed that dies this fast every time got its stream and could not keep
        it, which on this wall has meant the pipeline downstream of the feed
        bin -- preserved across restarts, and never flushed -- holding state
        from the generation before. Retrying cannot clear that, so the wall
        will sit in this loop indefinitely while the rest of it runs normally.
        """
        healthy_at = feed.healthy_at
        if healthy_at is None:
            # Never reached healthy: no stream at all, which the ordinary
            # restart line already describes.
            feed.short_lived_generations = 0
            return
        lifetime = time.monotonic() - healthy_at
        if lifetime > self.SHORT_GENERATION_SECONDS:
            if feed.branches_rebuilt:
                LOG.warning(
                    "feed %s: ran %.0fs after its branch was rebuilt, so the "
                    "rebuild cleared the wedge",
                    feed.config.name,
                    lifetime,
                )
                feed.branches_rebuilt = False
                feed.rebuilt_at_generation = None
            feed.short_lived_generations = 0
            return
        feed.short_lived_generations += 1
        if feed.short_lived_generations < self.SHORT_GENERATION_LIMIT:
            return
        LOG.error(
            "feed %s: connected and died within %.1fs on %d consecutive "
            "attempts; the stream reaches the wall but no video follows it. "
            "Retrying alone does not clear this",
            feed.config.name,
            lifetime,
            feed.short_lived_generations,
        )
        # Reported once per run of failures rather than on every attempt: the
        # condition persists, and repeating it every 30s buries the log the
        # way the warning it replaces already did.
        feed.short_lived_generations = 0
        if feed.branches_rebuilt:
            # Already tried, and the feed is wedging again. Say so plainly
            # rather than tearing the same branches down a second time.
            LOG.error(
                "feed %s: still wedging after a branch rebuild, so the stale "
                "state is not in the branch. Check the feed from this machine "
                "(gst-launch-1.0 rtspsrc location=... ! fakesink); if that "
                "plays, only restarting viewwall will clear it",
                feed.config.name,
            )
            return
        feed.branches_rebuilt = self._rebuild_feed_branches(feed)
        if feed.branches_rebuilt:
            feed.rebuilt_at_generation = feed.generation

    def _rebuild_feed_branches(self, feed: FeedRuntime) -> bool:
        """Recreate one feed's branch elements, leaving every other feed alone.

        The branch queue and videocrop are all that a feed restart does not
        already replace: the bin in front of them is rebuilt every generation
        and _show_viewport_offline() replaces the sink behind them on every
        outage. A wedge that survives both is therefore holding state here,
        and this is the last thing short of restarting the process.

        Every viewport carrying the feed is rebuilt, including one still
        showing a healthy feed beside the sick one. The pad released there is
        the inactive one -- the selector switched away when the feed failed --
        and a rotating viewport that skipped the repair would keep the wedged
        feed forever, silently showing one camera where two were configured,
        which is harder to notice than a black tile. Three failed restarts
        have already happened by this point, so the cost is paid rarely, and
        a healthy neighbour disturbed by it recovers through the same restart
        that works for every other transient fault.
        """
        rebuilt: list[str] = []
        for viewport in self.viewports.values():
            if feed.config.name not in viewport.config.feeds:
                continue
            try:
                self._rebuild_one_branch(viewport, feed.config.name)
            except RuntimeDependencyError as exc:
                # The graph is now missing a branch, and nothing else can put
                # it back, so this is fatal in the way a failed sink swap is.
                self._fatal(
                    f"feed {feed.config.name}: branch rebuild failed: {exc}"
                )
                return False
            rebuilt.append(f"viewport {viewport.config.index}")
        if not rebuilt:
            LOG.warning(
                "feed %s: no viewport could be rebuilt; every one showing this "
                "feed still has another feed on screen",
                feed.config.name,
            )
            return False
        LOG.warning(
            "feed %s: rebuilt the branch for %s; the next generation will show "
            "whether that cleared it",
            feed.config.name,
            ", ".join(rebuilt),
        )
        return True

    def _rebuild_one_branch(self, viewport: ViewportRuntime, feed_name: str) -> None:
        feed = self.feeds[feed_name]
        old_queue = viewport.branch_queues.get(feed_name)
        old_crop = viewport.branch_crops.get(feed_name)
        old_pad = viewport.selector_pads.get(feed_name)
        if old_queue is None or old_crop is None or old_pad is None:
            raise RuntimeDependencyError("branch elements are missing")

        # Block the tee's pad before touching anything. Releasing a request pad
        # the tee is actively pushing to leaves the tee flushing every buffer
        # it handles afterwards, and it never comes back: the branch relinks,
        # the pads report linked and active, and no video ever arrives. That
        # failure is indistinguishable from the wedge this repairs, so getting
        # it wrong here would look like the repair simply not working.
        old_tee_pad = viewport.branch_tee_pads.get(feed_name)
        probe = None
        if old_tee_pad is not None:
            probe = old_tee_pad.add_probe(
                self.Gst.PadProbeType.BLOCK_DOWNSTREAM | self.Gst.PadProbeType.IDLE,
                lambda pad, info: self.Gst.PadProbeReturn.OK,
            )

        for element in (old_queue, old_crop):
            element.set_locked_state(True)
            if element.set_state(self.Gst.State.NULL) == self.Gst.StateChangeReturn.FAILURE:
                raise RuntimeDependencyError(f"could not stop {element.get_name()}")

        if old_tee_pad is not None:
            old_tee_pad.unlink(old_queue.get_static_pad("sink"))
            if probe is not None:
                old_tee_pad.remove_probe(probe)
            feed.tee.release_request_pad(old_tee_pad)
        old_crop.get_static_pad("src").unlink(old_pad)
        viewport.selector.release_request_pad(old_pad)
        old_queue.unlink(old_crop)
        for element in (old_queue, old_crop):
            if not self.pipeline.remove(element):
                raise RuntimeDependencyError(f"could not remove {element.get_name()}")

        viewport.branch_generation += 1
        self._connect_one_branch(viewport, feed_name)
        # The crop values are per branch and the new videocrop has none.
        viewport.crop_values = None
        self._apply_viewport_crop(viewport)
        # Built into a pipeline that is already PLAYING, unlike the initial
        # branches, so the new elements have to be brought up to meet it.
        for element in (
            viewport.branch_queues[feed_name],
            viewport.branch_crops[feed_name],
        ):
            if not element.sync_state_with_parent():
                raise RuntimeDependencyError(
                    f"could not start {element.get_name()}"
                )

    def _request_feed_stop(self, feed_name: str, generation: int, reason: str) -> None:
        self.GLib.idle_add(self._stop_feed, feed_name, generation, reason)

    def _stop_feed(self, feed_name: str, generation: int, reason: str) -> bool:
        """Retire a feed permanently: retrying cannot fix its configuration."""
        if self._stopping:
            return False
        feed = self.feeds[feed_name]
        if feed.generation != generation or feed.state == "unsupported":
            return False
        LOG.error("feed %s: %s", feed_name, reason)
        feed.state = "unsupported"
        try:
            self._teardown_feed_attempt(feed)
        except RuntimeDependencyError as exc:
            self._fatal(str(exc))
            return False
        # Other viewports carry on; a viewport showing only this feed goes black rather
        # than holding a stale frame.
        self._select_alternate_for_failed_feed(feed_name)
        return False

    def _retry_feed(self, feed_name: str, generation: int) -> bool:
        if self._stopping:
            return False
        feed = self.feeds[feed_name]
        if feed.generation != generation or feed.state != "backoff":
            return False
        feed.retry_source_id = None
        try:
            self._start_feed_attempt(feed)
        except RuntimeDependencyError as exc:
            LOG.error("feed %s: restart failed: %s", feed_name, exc)
            feed.failures += 1
            try:
                self._teardown_feed_attempt(feed)
            except RuntimeDependencyError as teardown_exc:
                self._fatal(str(teardown_exc))
                return False
            feed.state = "backoff"
            self._schedule_feed_retry(feed)
        return False

    def _start_initial_feeds(self) -> None:
        for feed in self.feeds.values():
            try:
                self._start_feed_attempt(feed)
            except RuntimeDependencyError as exc:
                LOG.error("feed %s: initial start failed: %s", feed.config.name, exc)
                feed.failures += 1
                try:
                    self._teardown_feed_attempt(feed)
                except RuntimeDependencyError as teardown_exc:
                    self._fatal(str(teardown_exc))
                    return
                feed.state = "backoff"
                self._schedule_feed_retry(feed)

    def _on_feed_first_buffer(self, pad: Any, info: Any, identity: tuple[str, int]) -> Any:
        feed_name, generation = identity
        self.GLib.idle_add(self._mark_feed_healthy, feed_name, generation)
        return self.Gst.PadProbeReturn.REMOVE

    def _mark_feed_healthy(self, feed_name: str, generation: int) -> bool:
        if self._stopping:
            return False
        feed = self.feeds[feed_name]
        if feed.generation != generation or feed.state in ("backoff", "unsupported"):
            return False
        feed.state = "healthy"
        feed.healthy_at = time.monotonic()
        LOG.info("feed %s: video is healthy", feed_name)
        for viewport in self.viewports.values():
            active_healthy = (
                viewport.active_feed is not None
                and self.feeds[viewport.active_feed].state == "healthy"
            )
            if feed_name in viewport.config.feeds and not active_healthy:
                self._activate_viewport_feed(viewport, viewport.config.feeds.index(feed_name))
        feed.stable_source_id = self.GLib.timeout_add_seconds(
            self.FEED_STABLE_SECONDS,
            self._mark_feed_stable,
            feed_name,
            generation,
        )
        return False

    def _mark_feed_stable(self, feed_name: str, generation: int) -> bool:
        feed = self.feeds[feed_name]
        if feed.generation == generation and feed.state == "healthy":
            feed.failures = 0
            feed.short_lived_generations = 0
            feed.stable_source_id = None
        return False

    def _on_feed_event(self, pad: Any, info: Any, identity: tuple[str, int]) -> Any:
        event = info.get_event()
        if event is None:
            return self.Gst.PadProbeReturn.OK
        feed_name, generation = identity
        if event.type == self.Gst.EventType.EOS:
            self._request_feed_restart(feed_name, generation, "end of stream")
            return self.Gst.PadProbeReturn.DROP
        if event.type == self.Gst.EventType.CAPS:
            caps = event.parse_caps()
            if caps is not None and caps.get_size() > 0:
                structure = caps.get_structure(0)
                ok_width, width = structure.get_int("width")
                ok_height, height = structure.get_int("height")
                feed = self.feeds[feed_name]
                if (
                    feed.generation == generation
                    and ok_width
                    and ok_height
                    and width > 0
                    and height > 0
                ):
                    feed.source_size = (width, height)
                    for viewport in self.viewports.values():
                        selected = viewport.config.feeds[viewport.active_index]
                        if selected == feed_name:
                            self._apply_viewport_aspect(viewport, feed.source_size)
                ok_fps, fps_n, fps_d = structure.get_fraction("framerate")
                if feed.generation == generation and ok_fps and fps_n > 0:
                    feed.caps_fps_known = True
                    self._apply_feed_watchdog(feed, fps_n / max(fps_d, 1))
        return self.Gst.PadProbeReturn.OK

    def _on_feed_buffer(self, pad: Any, info: Any, identity: tuple[str, int]) -> Any:
        feed_name, generation = identity
        feed = self.feeds[feed_name]
        if feed.generation != generation:
            return self.Gst.PadProbeReturn.OK
        feed.decoded_frames += 1
        if feed.observed_fps_applied or feed.caps_fps_known:
            # A declared framerate is authoritative; measuring on top of it
            # would let a startup hiccup loosen a fast feed's watchdog.
            return self.Gst.PadProbeReturn.OK
        self._observe_feed_interval(feed, time.monotonic())
        return self.Gst.PadProbeReturn.OK

    def _observe_feed_interval(self, feed: FeedRuntime, now: float) -> None:
        """Track the gap between decoded frames, for feeds with no caps rate.

        Some cameras negotiate caps without a usable framerate, and those are
        exactly the slow ones a fixed watchdog punishes. Measuring the real
        interval covers them without asking the user to configure anything.
        """
        previous = feed.last_frame_at
        feed.last_frame_at = now
        if previous is None:
            return
        gap = now - previous
        if gap <= 0:
            return
        # A rolling maximum, so an occasional fast frame does not tighten the
        # watchdog back down on a slow feed.
        feed.max_frame_gap = max(feed.max_frame_gap or 0.0, gap)
        if feed.observed_fps_applied:
            return
        # 0.2s is a 5fps feed. Anything slower than that is where a fixed
        # watchdog starts to be wrong, and anything faster is already safe.
        if feed.max_frame_gap >= 0.2:
            self._apply_feed_watchdog(feed, 1.0 / feed.max_frame_gap)
            feed.observed_fps_applied = True

    def _apply_feed_watchdog(self, feed: FeedRuntime, fps: float) -> None:
        """Scale the stall watchdog to the framerate the feed actually sends.

        A stalled feed should be caught quickly, but "stalled" means something
        different at 3fps than at 30fps. Allowing a fixed number of missed
        frames keeps the meaning constant, so a slow camera is not restarted
        for being slow.
        """
        if feed.watchdog is None or fps <= 0:
            return
        timeout = int(self.STALL_FRAMES / fps * 1000)
        timeout = max(self.MIN_STALL_TIMEOUT_MS, timeout)
        current = feed.watchdog.get_property("timeout")
        if current == timeout and feed.watchdog_reported:
            return
        # Reported even when the value does not change, which happens when the
        # rate works out to the startup default -- 45 frames at 3fps is exactly
        # it. Such a feed logged nothing at all, and a feed missing from the
        # watchdog log is indistinguishable from one the scaling never reached.
        feed.watchdog.set_property("timeout", timeout)
        feed.watchdog_reported = True
        LOG.info(
            "feed %s: %.3g fps, stall watchdog set to %.1fs",
            feed.config.name,
            fps,
            timeout / 1000.0,
        )

    def _connect_feed_branches(self) -> None:
        for viewport in self.viewports.values():
            for feed_name in viewport.config.feeds:
                self._connect_one_branch(viewport, feed_name)

    def _connect_one_branch(self, viewport: ViewportRuntime, feed_name: str) -> None:
        """Wire one feed into one viewport, from the tee to the selector.

        Used for the initial build and again when a wedged branch is rebuilt,
        so the replacement is assembled by the same code as the original and
        cannot drift from it.
        """
        feed = self.feeds[feed_name]
        safe_viewport = viewport.config.name.replace("-", "_")
        safe_feed = feed_name.replace("-", "_")
        generation = viewport.branch_generation
        suffix = "" if generation == 0 else f"_{generation}"
        queue = self._element(
            "queue", f"queue_{safe_feed}_to_{safe_viewport}{suffix}"
        )
        # A single buffer per branch throttles the tee; 4 measured best.
        queue.set_property("max-size-buffers", 4)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        self.Gst.util_set_object_arg(queue, "leaky", "downstream")
        self.pipeline.add(queue)

        # One videocrop per branch, before the selector. It sees a
        # single decoder's caps for the lifetime of the branch, so the
        # cropped caps kmssink sizes its pool from never change and a
        # rotating viewport keeps its seam and its 1:1 plane.
        crop = self._element(
            "videocrop", f"crop_{safe_feed}_to_{safe_viewport}{suffix}"
        )
        self.pipeline.add(crop)
        viewport.branch_crops[feed_name] = crop
        viewport.branch_queues[feed_name] = queue

        tee_pad = feed.tee.request_pad_simple("src_%u")
        selector_pad = viewport.selector.request_pad_simple("sink_%u")
        if tee_pad is None or selector_pad is None:
            raise RuntimeDependencyError("could not allocate GStreamer request pad")
        if tee_pad.link(queue.get_static_pad("sink")) != self.Gst.PadLinkReturn.OK:
            raise RuntimeDependencyError(f"could not branch feed {feed_name}")
        self._link_many(queue, crop)
        if crop.get_static_pad("src").link(selector_pad) != self.Gst.PadLinkReturn.OK:
            raise RuntimeDependencyError(
                f"could not connect feed {feed_name} to viewport {viewport.config.name}"
            )
        viewport.selector_pads[feed_name] = selector_pad
        viewport.branch_tee_pads[feed_name] = tee_pad

    def _select_initial_feeds(self) -> None:
        for viewport in self.viewports.values():
            # The first listed feed. A viewport that rotates reaches the others
            # within one interval anyway, so choosing among them is not worth
            # a configuration option.
            initial = viewport.config.feeds[0]
            viewport.active_index = 0
            viewport.selector.set_property("active-pad", viewport.selector_pads[initial])
            self._show_viewport_offline(viewport)
            # No frames exist yet, so the plane remains black. Keeping the
            # selected branch open is nevertheless essential: allocation
            # queries must reach kmssink before videocrop sees its first
            # DMA_DRM buffer.
            viewport.valve.set_property("drop", False)
            if len(viewport.config.feeds) > 1 and viewport.config.rotate_seconds is not None:
                interval_ms = max(1, round(viewport.config.rotate_seconds * 1000))
                self.GLib.timeout_add(interval_ms, self._rotate_viewport, viewport.config.name)

    def _prime_offline_viewports_for_feed(self, feed_name: str) -> None:
        """Open selected empty branches before a decoder begins allocation."""
        for viewport in self.viewports.values():
            selected_feed = viewport.config.feeds[viewport.active_index]
            if viewport.active_feed is None and selected_feed == feed_name:
                viewport.selector.set_property(
                    "active-pad", viewport.selector_pads[feed_name]
                )
                viewport.sink.set_locked_state(False)
                if not viewport.sink.sync_state_with_parent():
                    raise RuntimeDependencyError(
                        f"could not prime KMS plane for viewport {viewport.config.name}"
                    )
                viewport.valve.set_property("drop", False)

    def _activate_viewport_feed(self, viewport: ViewportRuntime, index: int) -> None:
        previous_feed = viewport.active_feed
        viewport.active_index = index
        feed_name = viewport.config.feeds[index]
        # The sink is deliberately NOT replaced on a feed change. Each branch
        # crops upstream of the selector, so every branch presents kmssink the
        # same cropped caps and the pool it negotiated stays valid. Swapping
        # the sink here used to be needed when one shared crop renegotiated,
        # and it cost a visible ~2s black gap on every switch while the new
        # sink prerolled.
        # Set the destination-fill caps before the pad switch so the sticky CAPS
        # the selector forwards already carry the right pixel aspect ratio.
        self._apply_viewport_aspect(viewport, self.feeds[feed_name].source_size)
        viewport.selector.set_property("active-pad", viewport.selector_pads[feed_name])
        viewport.sink.set_locked_state(False)
        if not viewport.sink.sync_state_with_parent():
            self._fatal(f"could not enable KMS plane for viewport {viewport.config.name}")
            return
        # The valve stays open from here on. A closed valve would block the
        # newly selected branch's ALLOCATION query from reaching kmssink.
        viewport.valve.set_property("drop", False)
        viewport.active_feed = feed_name
        # Frames counted before the switch came from the previous feed. Left
        # in place they would be averaged with the new one's, and a viewport
        # rotating a 3fps and a 24fps camera would report a meaningless ~13.
        viewport.queued_frames = 0
        viewport.metrics_since = time.monotonic()
        viewport.metrics_rotated = True
        LOG.info(
            "viewport %d: selected feed %s%s",
            viewport.config.index,
            feed_name,
            "" if previous_feed is None else f" (was {previous_feed})",
        )

    def _replace_viewport_sink(self, viewport: ViewportRuntime) -> None:
        """Release a plane and leave a fresh, reusable sink behind it."""
        old_sink = viewport.sink
        old_sink.set_locked_state(True)
        state_result = old_sink.set_state(self.Gst.State.NULL)
        if state_result == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeDependencyError(
                f"could not disable KMS plane for viewport {viewport.config.name}"
            )
        viewport.output_queue.unlink(old_sink)
        if not self.pipeline.remove(old_sink):
            raise RuntimeDependencyError(
                f"could not remove stopped KMS sink for viewport {viewport.config.name}"
            )

        viewport.sink_generation += 1
        safe = viewport.config.name.replace("-", "_")
        new_sink = self._new_kms_sink(
            safe,
            viewport.plane_id,
            self.displays[viewport.display_name].connector_id,
            viewport.sink_generation,
        )
        self.pipeline.add(new_sink)
        if not viewport.output_queue.link(new_sink):
            raise RuntimeDependencyError(
                f"could not attach replacement KMS sink for viewport {viewport.config.name}"
            )
        if viewport.resolved is not None:
            rect = viewport.resolved.render
            if not self.GstVideo.VideoOverlay.set_render_rectangle(
                new_sink, rect.x, rect.y, rect.width, rect.height
            ):
                raise RuntimeDependencyError(
                    f"replacement KMS sink for viewport {viewport.config.name} "
                    "rejected its render rectangle"
                )
        if not new_sink.sync_state_with_parent():
            raise RuntimeDependencyError(
                f"could not start replacement KMS sink for viewport {viewport.config.name}"
            )
        viewport.sink = new_sink

    def _show_viewport_offline(self, viewport: ViewportRuntime) -> None:
        was_active = viewport.active_feed is not None
        viewport.valve.set_property("drop", True)
        if was_active:
            # kmssink closes and invalidates an externally supplied fd when it
            # enters NULL. Replace that spent instance so a recovered feed can
            # reuse the plane without reconstructing the complete pipeline.
            try:
                self._replace_viewport_sink(viewport)
            except RuntimeDependencyError as exc:
                self._fatal(str(exc))
                return
        viewport.active_feed = None
        # Name the feeds rather than only the viewport: this is the message that
        # says a camera went dark, and "viewport 3" alone does not say which.
        LOG.info(
            "viewport %d (%s): no healthy feed; KMS plane disabled",
            viewport.config.index,
            ", ".join(viewport.config.feeds),
        )

    def _next_healthy_feed_index(self, viewport: ViewportRuntime) -> int | None:
        count = len(viewport.config.feeds)
        for offset in range(1, count + 1):
            index = (viewport.active_index + offset) % count
            if self.feeds[viewport.config.feeds[index]].state == "healthy":
                return index
        return None

    def _select_alternate_for_failed_feed(self, feed_name: str) -> None:
        for viewport in self.viewports.values():
            if viewport.active_feed != feed_name:
                continue
            replacement = self._next_healthy_feed_index(viewport)
            if replacement is not None:
                self._activate_viewport_feed(viewport, replacement)
            else:
                self._show_viewport_offline(viewport)

    @staticmethod
    def _rtp_codec(encoding_name: str) -> str | None:
        normalized = encoding_name.upper().replace(".", "")
        return {"H264": "h264", "H265": "h265", "HEVC": "h265"}.get(normalized)

    def _decoder_factory(self, codec: str) -> tuple[str, bool]:
        if codec == "h265":
            # HEVC is supported only through the stateless hardware decoder.
            candidates = (("v4l2slh265dec", True),)
        else:
            # Pi 5 has no H.264 decoder, but can software-decode legacy feeds.
            candidates = (("v4l2h264dec", True), ("avdec_h264", False))

        for factory, is_hardware in candidates:
            if self.Gst.ElementFactory.find(factory) is not None:
                return factory, is_hardware
        choices = ", ".join(factory for factory, _is_hardware in candidates)
        raise RuntimeDependencyError(
            f"no supported decoder is available for {codec.upper()} "
            f"(looked for {choices})"
        )

    def _build_codec_branch(self, feed: FeedRuntime, codec: str) -> None:
        if feed.bin is None or feed.watchdog is None:
            raise RuntimeDependencyError(f"feed {feed.config.name} is no longer active")
        safe = feed.config.name.replace("-", "_")
        depay_factory, parser_factory = {
            "h264": ("rtph264depay", "h264parse"),
            "h265": ("rtph265depay", "h265parse"),
        }[codec]
        decoder_factory, is_hardware = self._decoder_factory(codec)
        depay = self._element(depay_factory, f"depay_{safe}_{codec}")
        parser = self._element(parser_factory, f"parse_{safe}_{codec}")
        decoder = self._element(decoder_factory, f"decode_{safe}_{codec}")

        self._set_if_present(parser, "config-interval", -1)
        if is_hardware:
            self._set_object_arg_if_present(decoder, "capture-io-mode", "dmabuf")

        for element in (depay, parser, decoder):
            feed.bin.add(element)
        self._link_many(depay, parser, decoder, feed.watchdog)
        for element in (depay, parser, decoder):
            if not element.sync_state_with_parent():
                raise RuntimeDependencyError(
                    f"could not start {element.get_name()} for feed {feed.config.name}"
                )

        feed.depay = depay
        feed.parser = parser
        feed.decoder = decoder
        feed.codec = codec
        if not is_hardware:
            LOG.warning(
                "feed %s: %s has no usable hardware decoder; using %s",
                feed.config.name,
                codec.upper(),
                decoder_factory,
            )

    def _on_rtsp_pad(
        self,
        source: Any,
        pad: Any,
        feed_name: str,
        generation: int,
    ) -> None:
        feed = self.feeds[feed_name]
        if (
            feed.generation != generation
            or feed.source != source
            or feed.state in ("backoff", "unsupported")
        ):
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if structure.get_name() != "application/x-rtp":
            return
        media = (structure.get_string("media") or "").lower()
        encoding = (structure.get_string("encoding-name") or "").upper()
        if media == "video" and _offers_srtp(structure):
            # The server negotiated SRTP, so the payload is encrypted and the
            # depayloader would fail on its first buffer with "The stream is in
            # the wrong format". Recovery would read that as a transient fault
            # and reconnect forever, so stop the feed with the real reason.
            #
            # This is detected from the negotiated SDP rather than the request
            # URI: how SRTP gets asked for is server-specific (UniFi Protect
            # uses an "?enableSrtp" query parameter, others differ), but the
            # SAVP profile and RFC 4568 key attribute are standard.
            self._request_feed_stop(
                feed_name,
                generation,
                "server negotiated SRTP, which Viewwall cannot decrypt; "
                "request an unencrypted-media stream instead",
            )
            return
        if media == "video" and not feed.video_linked:
            codec = self._rtp_codec(encoding)
            if codec is None:
                LOG.error(
                    "feed %s: unsupported RTP video encoding %s",
                    feed.config.name,
                    encoding or "<unknown>",
                )
                self._request_feed_restart(feed_name, generation, "unsupported video encoding")
                return
            try:
                self._build_codec_branch(feed, codec)
            except RuntimeDependencyError as exc:
                LOG.error("feed %s: %s", feed.config.name, exc)
                self._request_feed_restart(feed_name, generation, str(exc))
                return
            assert feed.depay is not None
            result = pad.link(feed.depay.get_static_pad("sink"))
            if result == self.Gst.PadLinkReturn.OK:
                feed.video_linked = True
                LOG.info(
                    "feed %s: %s video connected via %s",
                    feed.config.name,
                    codec.upper(),
                    feed.decoder.get_factory().get_name(),
                )
            else:
                LOG.error(
                    "feed %s: could not link %s RTP pad (%s)",
                    feed.config.name,
                    codec.upper(),
                    result.value_nick,
                )
                self._request_feed_restart(feed_name, generation, "RTP pad link failure")
        elif media == "audio" and not feed.audio_linked:
            result = pad.link(feed.audio_queue.get_static_pad("sink"))
            if result == self.Gst.PadLinkReturn.OK:
                feed.audio_linked = True
                LOG.debug("feed %s: audio is being discarded", feed.config.name)

    def _apply_viewport_crop(self, viewport: ViewportRuntime) -> None:
        if viewport.resolved is None:
            return
        # Clip the same count of source-edge pixels as the destination inset,
        # so the destination matches the cropped source exactly and KMS scans
        # the plane out 1:1 instead of scaling it. A full wall depends on this:
        # nine scaling planes exhaust the Pi 3's KMS resources, nine 1:1 planes
        # do not.
        #
        # This is deliberately static: changing videocrop after DMA_DRM caps
        # negotiation can make GStreamer's generic video info disagree with the
        # decoder's concrete GstVideoMeta format.
        crop = SourceCrop(
            left=viewport.resolved.insets.left,
            top=viewport.resolved.insets.top,
            right=viewport.resolved.insets.right,
            bottom=viewport.resolved.insets.bottom,
        )
        if crop == viewport.crop_values:
            return
        for branch_crop in viewport.branch_crops.values():
            branch_crop.set_property("left", crop.left)
            branch_crop.set_property("top", crop.top)
            branch_crop.set_property("right", crop.right)
            branch_crop.set_property("bottom", crop.bottom)
        viewport.crop_values = crop
        LOG.debug(
            "viewport %d: source crop l=%d t=%d r=%d b=%d",
            viewport.config.index,
            crop.left,
            crop.top,
            crop.right,
            crop.bottom,
        )

    def _apply_viewport_aspect(
        self,
        viewport: ViewportRuntime,
        source_size: tuple[int, int] | None,
    ) -> None:
        if viewport.resolved is None or viewport.crop_values is None or source_size is None:
            return
        source_width = (
            source_size[0]
            - viewport.crop_values.left
            - viewport.crop_values.right
        )
        source_height = (
            source_size[1]
            - viewport.crop_values.top
            - viewport.crop_values.bottom
        )
        if source_width <= 0 or source_height <= 0:
            self._fatal(f"source is too small for viewport {viewport.config.name} crop")
            return
        render = viewport.resolved.render
        ratio = Fraction(
            render.width * source_height,
            render.height * source_width,
        )
        if ratio == viewport.pixel_aspect_ratio:
            return
        caps = self.Gst.Caps.from_string(
            "video/x-raw,"
            f"pixel-aspect-ratio={ratio.numerator}/{ratio.denominator}"
        )
        viewport.aspect.set_property("caps", caps)
        viewport.pixel_aspect_ratio = ratio
        LOG.debug(
            "viewport %d: pixel aspect ratio %d/%d",
            viewport.config.index,
            ratio.numerator,
            ratio.denominator,
        )

    def _apply_layout(self, display_name: str | None = None) -> None:
        """Place viewports, on one display or on all of them.

        Each display is its own 0..1 canvas resolved against its own mode and
        spacing, so a mode change on one leaves the others untouched.
        """
        for display_config in self.config.displays:
            if display_name is not None and display_config.name != display_name:
                continue
            state = self.displays[display_config.name]
            viewports = self.config.viewports_for(display_config)
            resolved = resolve_layout(
                viewports,
                state.width,
                state.height,
                self.config.layout_for(display_config),
            )
            for viewport_config in viewports:
                name = viewport_config.name
                viewport = self.viewports[name]
                viewport.resolved = resolved[name]
                rect = viewport.resolved.render
                if not self.GstVideo.VideoOverlay.set_render_rectangle(
                    viewport.sink, rect.x, rect.y, rect.width, rect.height
                ):
                    raise RuntimeDependencyError(
                        f"kmssink for viewport {name} rejected its render rectangle"
                    )
                self._apply_viewport_crop(viewport)
                selected_feed = viewport.config.feeds[viewport.active_index]
                self._apply_viewport_aspect(
                    viewport,
                    self.feeds[selected_feed].source_size,
                )
                LOG.info(
                    # The display is worth naming only when there is a choice
                    # of them; on one screen it repeats on every line.
                    "viewport %d (%s): %splane=%d rectangle=%d,%d %dx%d",
                    viewport_config.index,
                    ", ".join(viewport_config.feeds),
                    ""
                    if len(self.config.displays) == 1
                    else f"display={display_config.name} ",
                    viewport.plane_id,
                    rect.x,
                    rect.y,
                    rect.width,
                    rect.height,
                )

    def _rotate_viewport(self, viewport_name: str) -> bool:
        if self._stopping:
            return False
        viewport = self.viewports[viewport_name]
        next_index = self._next_healthy_feed_index(viewport)
        if next_index is not None:
            self._activate_viewport_feed(viewport, next_index)
        return True

    def _poll_display(self) -> bool:
        if self._stopping:
            return False
        # Cheap check first. A full probe shells out to kmsprint, which opens
        # the DRM device and contends with this process's own page flips: 0.1s
        # with the wall stopped, a median of 7s with nine planes scanning out.
        # Polling that every couple of seconds meant the probe usually timed
        # out, so read the mode from sysfs and only probe when it changed.
        pinned = {
            display.name
            for display in self.config.displays
            if display.width is not None
        }
        if len(pinned) < len(self.config.displays):
            modes = current_modes()
            if modes and all(
                modes.get(self.displays[display.name].connector_id)
                == (
                    self.displays[display.name].width,
                    self.displays[display.name].height,
                )
                for display in self.config.displays
                if display.name not in pinned
            ):
                return True
        demand = {
            display.name: len(self.config.viewports_for(display))
            for display in self.config.displays
        }
        try:
            current = detect_displays(self.config.displays, demand)
        except Exception as exc:  # a disconnected display should not terminate active streams
            LOG.warning("display probe failed: %s", exc)
            return True
        for display in self.config.displays:
            was = self.displays[display.name]
            now = current[display.name]
            if (now.width, now.height) == (was.width, was.height):
                continue
            LOG.info(
                "display %s mode changed from %dx%d to %dx%d",
                display.name,
                was.width,
                was.height,
                now.width,
                now.height,
            )
            # Keep the planes already handed out: they are bound to sinks that
            # are mid-flight, and a re-probe may order them differently.
            self.displays[display.name] = DisplayState(
                connector_id=now.connector_id,
                crtc_index=now.crtc_index,
                crtc_id=now.crtc_id,
                width=now.width,
                height=now.height,
                plane_ids=was.plane_ids,
            )
            try:
                self._apply_layout(display.name)
            except RuntimeDependencyError as exc:
                self._fatal(str(exc))
                return False
        return True

    def _feed_identity_for_source(self, source: Any) -> tuple[str, int] | None:
        current = source
        while current is not None:
            get_name = getattr(current, "get_name", None)
            if get_name is not None:
                identity = self._feed_bins.get(get_name())
                if identity is not None:
                    return identity
            get_parent = getattr(current, "get_parent", None)
            current = get_parent() if get_parent is not None else None
        return None

    def _fatal(self, reason: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = reason
            LOG.error("fatal wall failure: %s", reason)
            if "resource error" in reason.lower():
                # kmssink reports this when the display controller cannot add
                # another plane to its composition. kmsprint happily lists more
                # free planes than can actually scan out at once, so the viewport
                # count is what has to come down.
                LOG.error(
                    "viewport count %d may exceed the planes this display "
                    "controller can scan out at once (a Raspberry Pi 3 manages "
                    "5); see the plane-budget section of DESIGN.md",
                    len(self.config.viewports),
                )
        self.stop()

    def _on_bus_message(self, bus: Any, message: Any) -> None:
        if message.type == self.Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source_name = message.src.get_name() if message.src is not None else "unknown"
            LOG.error("GStreamer error from %s: %s", source_name, error.message)
            if debug:
                LOG.debug("GStreamer error details: %s", debug)
            identity = self._feed_identity_for_source(message.src)
            if identity is None:
                self._fatal(f"GStreamer error from {source_name}: {error.message}")
            else:
                self._request_feed_restart(
                    *identity,
                    f"error from {source_name}: {error.message}",
                )
        elif message.type == self.Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "GstRTSPSrcTimeout":
                identity = self._feed_identity_for_source(message.src)
                if identity is not None:
                    self._request_feed_restart(*identity, "RTSP session timeout")
        elif message.type == self.Gst.MessageType.EOS:
            self._fatal("unexpected end of the complete pipeline")
        elif message.type == self.Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            source_name = message.src.get_name() if message.src is not None else "unknown"
            if self._LATE_BUFFER_WARNING in warning.message:
                # GstBaseSink counts late buffers over a window and warns from
                # that alone. A camera whose decoder holds frames and releases
                # them in bursts trips it continuously while losing nothing:
                # measured over 44 one-minute samples, the viewport that emits this
                # averaged 29.86fps from a 30fps source. Left at WARNING it was
                # the only warning the wall ever produced, 1279 in a day, which
                # trains the reader to ignore the channel. The metrics measure
                # the same thing honestly, so this drops to DEBUG rather than
                # being discarded.
                LOG.debug("GStreamer warning from %s: %s", source_name, warning.message)
            else:
                LOG.warning("GStreamer warning from %s: %s", source_name, warning.message)

    @staticmethod
    def _notify_systemd(message: str) -> bool:
        address = os.environ.get("NOTIFY_SOCKET")
        if not address:
            return False
        if address.startswith("@"):
            address = "\0" + address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify_socket:
                notify_socket.connect(address)
                notify_socket.sendall(message.encode("utf-8"))
        except OSError as exc:
            LOG.warning("could not notify systemd: %s", exc)
            return False
        return True

    def _enable_systemd_watchdog(self) -> None:
        watchdog_pid = os.environ.get("WATCHDOG_PID")
        if watchdog_pid is not None:
            try:
                if int(watchdog_pid) != os.getpid():
                    return
            except ValueError:
                return
        try:
            watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        except ValueError:
            return
        if watchdog_usec <= 0:
            return
        interval_ms = max(1000, watchdog_usec // 3000)
        self.GLib.timeout_add(interval_ms, self._systemd_watchdog_tick)

    def _systemd_watchdog_tick(self) -> bool:
        if self._stopping:
            return False
        self._notify_systemd("WATCHDOG=1")
        return True

    def _on_viewport_buffer(self, pad: Any, info: Any, viewport_name: str) -> Any:
        viewport = self.viewports.get(viewport_name)
        if viewport is not None:
            viewport.queued_frames += 1
        return self.Gst.PadProbeReturn.OK

    def _queue_delay_ms(self, queue: Any) -> float | None:
        """How much video is waiting in a queue, in milliseconds.

        This is the direct read on lag: a queue holding 250ms of video is a
        viewport a quarter-second behind live.
        """
        if queue is None:
            return None
        try:
            level_ns = queue.get_property("current-level-time")
        except (TypeError, AttributeError):
            return None
        return level_ns / 1_000_000.0

    def _sink_presented(self, sink: Any) -> tuple[int, int] | None:
        """Frames the sink has put on screen, and frames it threw away.

        queued_frames counts buffers leaving the output queue, which is one
        step short: a buffer the sink drops has still left the queue. These
        are basesink's own totals, taken after that decision, and so are the
        closest thing available to what actually reached the panel.

        Note that basesink's "rendered" means frames it put on the plane,
        which is what this module calls presented. The queue-side count is
        named queued throughout to keep the two apart.
        """
        if sink is None:
            return None
        try:
            stats = sink.get_property("stats")
        except (TypeError, AttributeError):
            return None
        if stats is None:
            return None
        try:
            return int(stats.get_value("rendered")), int(stats.get_value("dropped"))
        except (TypeError, AttributeError, ValueError):
            return None

    def _start_metrics(self) -> None:
        if not self.config.metrics.enabled:
            return
        interval_ms = max(1000, round(self.config.metrics.interval_seconds * 1000))
        self._metrics_sampled_at = time.monotonic()
        self.GLib.timeout_add(interval_ms, self._report_metrics)

    def _report_metrics(self) -> bool:
        """Log queued and presented framerates, and queue occupancy.

        Emitted every interval whether or not anything looks wrong, because the
        useful question is retrospective: was the wall dropping frames an hour
        ago? Fields are attached structurally so they can be aggregated without
        parsing the message text.
        """
        if self._stopping:
            return False
        now = time.monotonic()
        elapsed = now - self._metrics_sampled_at
        self._metrics_sampled_at = now
        if elapsed <= 0:
            return True

        # Read and reset every feed once up front: a feed can appear in more
        # than one viewport, and zeroing it inside the viewport loop would leave the
        # second viewport reporting nothing.
        showing = 0
        decoded_rates = {}
        for name, feed in self.feeds.items():
            decoded_rates[name] = feed.decoded_frames / elapsed
            feed.decoded_frames = 0

        for viewport in self.viewports.values():
            queued = viewport.queued_frames
            viewport.queued_frames = 0
            # A viewport that switched feeds mid-interval has been counting for
            # less than the full interval, so divide by its own window.
            window = elapsed
            if viewport.metrics_since is not None:
                window = max(now - viewport.metrics_since, 0.0)
            viewport.metrics_since = now
            # A rotation landing just before the report leaves a window too
            # short to measure a rate from: one stray buffer over 2ms reads as
            # hundreds of fps. Report the count instead of inventing a rate.
            measurable = window >= self.MIN_METRICS_WINDOW_S
            # Whether the viewport switched feeds during this interval. If it
            # did, fps covers only the time since the switch while the decoder
            # ran all interval, so the two rates are not comparable.
            rotated = viewport.metrics_rotated
            viewport.metrics_rotated = False
            # active_index, not active_feed: a viewport primed at startup and never
            # rotated renders correctly while active_feed is still None, since
            # _prime_offline_viewports_for_feed() opens the branch without setting
            # it. The selected index is accurate in both paths.
            feed_name = viewport.config.feeds[viewport.active_index]
            feed = self.feeds.get(feed_name)
            queue_ms = self._queue_delay_ms(viewport.output_queue)
            # Presented rate, which is not the same as the queued rate above
            # and is the one the eye sees. Each kmssink issues its own
            # drmModeSetPlane and the VC4 retires one per vblank, so nine
            # planes share 60 updates a second however fast the decoders run.
            # Reported next to fps so the two can be compared directly: a wide
            # gap is the plane path, not the network or the decoder.
            presented = self._sink_presented(viewport.sink)
            presented_fps: float | None = None
            dropped_delta: int | None = None
            if presented is not None:
                total, dropped = presented
                # A replaced sink starts its counters again, so a total below
                # what was seen last time is a new sink rather than a wrap.
                if total >= viewport.presented_total:
                    presented_fps = (total - viewport.presented_total) / window
                    dropped_delta = dropped - viewport.dropped_total
                viewport.presented_total = total
                viewport.dropped_total = dropped
            fields = {
                "VW_VIEWPORT": viewport.config.index,
                "VW_FEED": feed_name,
                "VW_STATE": feed.state if feed is not None else "none",
                "VW_QUEUED_FPS": f"{queued / window:.1f}" if measurable else "-",
                "VW_DECODED_FPS": f"{decoded_rates.get(feed_name, 0.0):.1f}",
                "VW_WINDOW_S": f"{window:.1f}",
            }
            if presented_fps is not None and measurable:
                fields["VW_PRESENTED_FPS"] = f"{presented_fps:.1f}"
            if dropped_delta:
                fields["VW_SINK_DROPPED"] = str(dropped_delta)
            if not measurable:
                # So the dash is not read as a dead viewport.
                fields["VW_FRAMES"] = str(queued)
            if rotated:
                # Names why, so the line is not read as dropped frames.
                fields["VW_ROTATED"] = "1"
            if queue_ms is not None:
                fields["VW_QUEUE_MS"] = f"{queue_ms:.0f}"
            LOG.info("metrics %s", format_fields(fields), extra=fields)
            if queued:
                showing += 1
        self._report_wall_health(showing)
        return True

    def _report_wall_health(self, showing: int) -> None:
        """Say so when the whole wall goes dark, and when it comes back.

        A single black viewport is ordinary -- one camera is unplugged, and its own
        log line says so. Every viewport black at once is a different condition
        with a common cause: the NVR is down, the credentials changed, or the
        transport cannot carry the streams. Nothing distinguished the two, so a
        wall showing nothing reported "Camera wall running" to systemd
        indefinitely, and the only evidence was INFO-level metrics somebody had
        to read.

        Logged on the transition rather than every interval, so a long outage
        is one error and one recovery rather than a repeating line.
        """
        dark = showing == 0 and bool(self.viewports)
        if dark == self._wall_dark:
            return
        self._wall_dark = dark
        if dark:
            LOG.error(
                "no viewport is showing video; all %d feeds are unavailable",
                len(self.feeds),
            )
            self._notify_systemd("STATUS=No video: all feeds unavailable")
        else:
            LOG.info("video restored: %d of %d viewports showing", showing, len(self.viewports))
            self._notify_systemd("STATUS=Camera wall running")

    def _signal_stop(self, *_args: object) -> bool:
        self.stop()
        return False

    def run(self) -> None:
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGINT, self._signal_stop)
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._signal_stop)
        poll_seconds = max(1, round(self.config.drm.poll_interval_seconds))
        self.GLib.timeout_add_seconds(poll_seconds, self._poll_display)
        # Assemble initial feed bins while the complete graph is still NULL.
        # In particular, videocrop and kmssink must negotiate DMA_DRM together
        # during the first state transition; hot-adding the initial decoders to
        # an already-PLAYING output graph can leave videocrop with generic
        # DMA_DRM video info that disagrees with the decoder's concrete meta.
        self._start_initial_feeds()
        if self._fatal_error is not None:
            self.close()
            raise RuntimeDependencyError(self._fatal_error)
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeDependencyError("GStreamer pipeline refused PLAYING state")
        self._enable_systemd_watchdog()
        self._start_metrics()
        self._notify_systemd("READY=1\nSTATUS=Camera wall running")
        single = len(self.config.displays) == 1
        LOG.info(
            # The connector id is named so that a machine with more than one
            # display shows which output was chosen, without having to run
            # kmsprint and guess.
            # Each display is one clause carrying its own viewport count, so the
            # counts do not have to be reconciled against a total up front.
            "camera wall started: %d feeds on %s",
            len(self.feeds),
            ", ".join(
                "%s%d viewports at %dx%d (connector %d)"
                % (
                    "" if single else f"{display.name}: ",
                    len(self.config.viewports_for(display)),
                    self.displays[display.name].width,
                    self.displays[display.name].height,
                    self.displays[display.name].connector_id,
                )
                for display in self.config.displays
            ),
        )
        try:
            self.loop.run()
        finally:
            self._notify_systemd("STOPPING=1")
            self.close()
        if self._fatal_error is not None:
            raise RuntimeDependencyError(self._fatal_error)

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.loop.quit()

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
        self._retired_feed_bins.clear()
        if self.drm_fd >= 0:
            os.close(self.drm_fd)
            self.drm_fd = -1
