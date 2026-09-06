# Viewwall internals

Design notes and measurements, kept out of the README so it can stay short.
Everything here was verified on a Raspberry Pi 3 Model B+ running Raspberry Pi
OS Trixie with nine UniFi Protect feeds.

## Design constraints

These are choices, not accidents, and each rules out an otherwise obvious
implementation:

- **No compositor.** No X11, Wayland, desktop environment or software video
  compositor. Each viewport scans out from its own KMS overlay plane, so nine feeds
  never pass through a mixer.
- **No pixel processing in Python.** Python builds and supervises a native
  GStreamer graph and never maps, copies, scales or inspects video. Any
  operation on the picture is done by an element or by KMS.
- **Decode each feed once**, even when it is a candidate for several viewports.
- **Stretch to fill the viewport**, deliberately ignoring the camera's own aspect
  ratio. This avoids pillarboxing an odd-shaped doorbell camera, and is done as
  caps metadata rather than by scaling in software.
- **Seams by clipping, not by drawing.** No border overlay planes and no
  shrinking of a full frame into a smaller destination.
- **Never software-decode HEVC.** A feed viewwall cannot decode in hardware is
  refused with a clear message rather than silently melting the CPU. Hardware
  H.264 (`v4l2h264dec`) is preferred and falls back to `avdec_h264` with a
  warning; H.265 requires `v4l2slh265dec`.

### Codec support by model

A Pi 3 has no HEVC decoder at all, so H.265 feeds are refused there:

```sh
vcgencmd codec_enabled H265               # disabled
v4l2-ctl -d /dev/video10 --list-formats-out
# MPG4, H264, MJPG, H263                  -- no HEVC
gst-inspect-1.0 v4l2slh265dec             # absent
```

H.265 is an option only on a Pi 4 (hardware HEVC) or a Pi 5.

## Media path

```text
UniFi Protect RTSP
  -> rtspsrc (video selected explicitly; audio discarded)
  -> codec detected from the RTSP SDP
  -> rtph264depay / h264parse / v4l2h264dec
     or rtph265depay / h265parse / v4l2slh265dec
  -> watchdog (decoded-frame stall detection)
  -> DMA-BUF
  -> tee
  -> per-branch videocrop (before the selector; keeps the plane 1:1)
  -> per-viewport input-selector
  -> valve (closed while the viewport has no healthy feed)
  -> pixel-aspect metadata adjusted to fill the viewport
  -> leaky output queue
  -> kmssink / KMS overlay plane
```

Python only constructs and controls this native GStreamer graph. It does not
map, copy, scale, or inspect video pixels. A feed is decoded once even when it
is a candidate for more than one viewport.

All feeds named by rotating viewports remain in `PLAYING` state. Rotation changes
an `input-selector` pad, avoiding RTSP reconnection and keyframe warm-up.

## Recovery model

The RTSP/depay/parser/decoder portion of every feed lives in an independently
replaceable GStreamer bin. Its output queue, tee, viewport selectors, and KMS
planes remain in place when the camera or decoder fails.

`rtspsrc` supplies RTSP keepalives and connection timeouts. A native
GStreamer `watchdog` after each decoder reports a failure when no decoded
frames arrive. Its timeout is a frame count rather than a fixed delay: 45
missed frames at the rate the feed actually sends, floored at 5 seconds. A
fixed delay cannot mean the same thing at both ends of the range, since 15
seconds is a long stall for a 30fps camera and less than one frame for a
camera sending every 20 seconds. The rate comes from the negotiated caps, or
is measured from the arriving frames when the caps omit it, so a slow camera
is not restarted merely for being slow. Feeds start at 15 seconds until a rate
is known. Viewwall also catches feed EOS and RTSP session timeout messages.
Any of these conditions tears down only the affected feed bin and recreates it
after a jittered 1, 2, 5, 10, then 30 second backoff. The 30 second delay is
retried indefinitely and the backoff resets after one minute of healthy video.

Rotating viewports skip feeds that are starting or in backoff and immediately use
another healthy candidate when possible. A viewport with no healthy candidate
closes a valve in that output branch and disables its KMS plane instead of
retaining a misleading stale camera frame. The display background shows
through as black. This needs no decoder, extra plane, synthetic video buffer,
or additional RTSP connection.

Errors in the persistent output graph, DRM, or KMS are global failures:
Viewwall exits non-zero and systemd reconstructs the complete wall. The
service also uses systemd's process watchdog to recover if the GLib control
loop hangs.

## Layout model

Viewports use normalized rectangles. Each coordinate is one exact fraction,
`"N/D"`, with the denominator optional so a whole number is written `0` or `1`
rather than `"0/1"`:

```toml
# viewport 1: upper left ninth
[[viewports]]
x = 0
y = 0
width = "1/3"
height = "1/3"
feeds = ["porch"]

# viewport 2: full-height strip down the right
[[viewports]]
x = "2/3"
y = 0
width = "1/3"
height = 1
feeds = ["doorbell", "chicken_run"]
rotate_seconds = 8   # optional; this is the default
```

Viewports have no names. Logs and metrics identify one by its position in the
file, counting from 1, and name the feed it is showing alongside.

There is no grid assumption. Viewports may be differently sized, staggered, or
overlapping as long as every rectangle remains within the normalized display.
Viewwall discovers the active KMS mode and resolves shared fractional edges to
the same pixel coordinate, including modes that are not evenly divisible.

The display is polled for mode changes. Viewport rectangles are reapplied when the
resolution changes.

### Plane budget

Each viewport gets its own KMS overlay plane. The limit is not the number of planes
but how many of them **KMS has to scale**.

On a Raspberry Pi 3 (VC4, 1920x1080@60):

- Nine planes scanning out **1:1** (source rectangle equal to destination
  rectangle) are stable. This is the normal 3x3 wall.
- Only about **five scaling** planes work. A sixth makes its `kmssink` fail
  with "GStreamer encountered a general resource error", which is a fatal wall
  failure, so the process exits and systemd restart-loops.

The VC4 HVS has far less scaler capacity than plane capacity. `kmsprint -l`
shows both rectangles per plane, so it tells you which planes are scaling:

```sh
kmsprint -l | grep fb-id
# 0,0 639x359 -> 640,0 639x359   <- 1:1, cheap
# 0,0 640x360 -> 640,0 639x359   <- scaled, expensive
```

Judge stability by whether `NRestarts` stays flat, not by a single `kmsprint`:
a wall that exceeds the scaler budget reaches all nine planes and then dies, so
a point-in-time check can look healthy moments before a failure.

### Background

Only the viewport rectangles are ever drawn. Below them the primary plane still
holds the framebuffer console, which shows through the gaps, the outer margin,
and any viewport whose plane is disabled because every feed behind it is down.
`drm.background` paints over it.

It is a **modeset**, not another plane, and that is the only reason it fits.
Measured on a Pi 3 at 1920x1080:

| Background as | Result |
|---|---|
| Overlay plane, full-screen 1:1 | ENOSPC beside 3 viewports; works only with 2 |
| Overlay plane, 640x360 scaled up | ENOSPC beside 5 viewports |
| Overlay plane, 640x360 rendered 1:1 | Fits beside 5 viewports |
| Modeset (`force-modesetting=true`) | Fits beside all nine, framerates unchanged |

A full-screen overlay costs roughly six 640x360 viewports of HVS budget. The
cost follows plane **width** across every scanline, so no pixel format or
buffer size avoids it -- the 640x360 rows above differ only in whether the HVS
must scale, and the full-screen 1:1 row shows size alone is enough to fail.
`force-modesetting` hands the buffer to the CRTC instead, which never enters
plane compositing at all. While it runs, `kmsprint -l` shows the primary with
no `fb-id`.

`force-modesetting` is load-bearing for a second reason: without it kmssink
picks the first free **overlay** rather than erroring, quietly taking a plane a
viewport needs.

The console returns on exit, including after `kill -9`: the kernel restores the
CRTC when the DRM fd closes. `restore-crtc=false` only stops kmssink restoring
the mode itself.

A viewport with no healthy feed now shows the background rather than the
console, so a dark tile and a camera pointed at a dark room look alike. Setting
`background` to something other than black distinguishes them.

### Why there is no lateness metric

A viewport losing frames could be losing them two ways: buffers timestamped
wrongly arrive far behind the clock, or buffers merely waiting their turn for
one of the CRTC's 60 commit slots a second are late by something near that
interval. basesink drops both past `max-lateness`, which defaults to 5ms.

Instrumenting this was tried and removed. Two findings came out of it.

**It is expensive.** Measuring every buffer cost ten percentage points of a Pi
3 core, 88.6% to 98.8%, on a wall already CPU bound. Not the arithmetic but
the crossings into C around it: `get_buffer`, `get_clock`, `get_base_time` and
reading `buffer.pts` are each PyGObject round trips costing microseconds, and
a probe on every buffer of every viewport runs ~230 times a second. Sampling
one buffer in sixteen recovered six of the ten points; the rest was the
sampling counter itself, a Python call on every buffer whatever it decides.

**It does not discriminate.** Over 27 intervals on a viewport that alternated
between 1fps and 7fps, mean lateness was 114ms while sick and 79ms while
healthy, but the ranges overlap: 79-254 against 73-83. A single interval
cannot be judged from it. `queue_ms` separates the same intervals far better,
73ms against 805ms, and costs one property read per viewport per minute.

The finding that survives is a negative one: a viewport recovered from 1.9fps
to 7.0fps while its mean lateness stayed at 89ms, so whatever such a viewport
is doing, it is not waiting on late buffers. What it is doing instead did not
come from this metric.

### Seams

```toml
[display_defaults]
gap_px = 1
outer_margin_px = 0
```

No border overlay planes are created. For a shared seam, the preceding viewport's
destination is inset and `videocrop` clips the matching number of source-edge
pixels. That is what keeps a seam free: the cropped source matches the inset
destination exactly, so the plane still scans out 1:1 rather than being scaled.

At a native 1920x1080 3x3 layout, a 640x360 source becomes a 639x359 source
crop rendered into a 639x359 destination.

**Placement matters.** Each *branch* gets its own `videocrop`, upstream of the
viewport's `input-selector`, in `_connect_feed_branches()`. A single crop placed
after the selector would be shared by every branch and would have to
renegotiate whenever the active feed changed: `kmssink` sizes its buffer pool
from the cropped caps, so the switch made imports fail with
`gst_video_frame_map_id: assertion info->finfo->format == meta->format`,
`kmssink` fell back to `copy_to_dumb_buffer`, and the RTSP source died with
`Internal data stream error`. With one crop per branch, each faces exactly one
decoder for the life of the branch and never renegotiates, so **rotating viewports
keep both their seam and their 1:1 plane** and no sink has to be rebuilt on a
switch.

Attaching `GstVideoCropMeta` from a pad probe was tried as a way to keep the
seam without touching caps. It does not work on this build: `kmssink`
references the crop-meta API but does not narrow its source rectangle from it,
so no seam appears.

Current `kmssink` always preserves the display aspect ratio it receives. To
make every feed fill its configured rectangle, Viewwall sets a per-feed
`pixel-aspect-ratio` immediately before the sink. This is a caps-only operation:
it does not scale, map, or copy the DMA-BUF in software.

The last column and row retain the display's outer edges unless
`outer_margin_px` is set.

## Framerate

Every viewport renders at its camera's source rate. Measured on a Pi 3 with nine
feeds, 1920x1080@60:

| Viewport | Source | Rendered |
|---|---|---|
| 1 | 24 | 23.7 |
| 2 | 30 | 29.6 |
| 3 | 30 | 29.6 |
| 4 | 24 | 23.8 |
| 5 | 24 | 23.7 |
| 6 | 15 | 14.9 |
| 7 | 30 | 29.7 |
| 8 | 24 | 23.8 |
| 9 | 24 | 23.9 |

Two settings matter, both found by measurement rather than guesswork:

**`kmssink qos=false`.** This is the big one. `kmssink` presents at most one
buffer per vblank, so with nine planes on a 60Hz output it decides it is
overrunning, logs "a lot of buffers are being dropped", and sends QoS events
upstream. `v4l2h264dec` honours them and discards frames *before decoding*.
With QoS on, feeds ran at 65% of source rate on average and the worst sat at
15%; with it off, 99% across the board. Late frames are still dropped, but by
the leaky queues at the display end rather than by throwing away work already
pulled off the network.

**Queue depth.** The per-viewport output queue holds 32 buffers and each per-branch
queue 4; the decode queue holds 8. The original 2/1/3 starved the sinks and the
slower feeds. Deeper than this does not help and only adds latency.

A full 32-buffer output queue is about a second of standing latency at 30fps,
so the depth was retested at 8 once the metrics made it visible, on the theory
that it might have been compensating for the QoS behaviour rather than for
anything real. It was not: at 8, `kmssink` resumes logging "a lot of buffers
are being dropped" once a second, which is the condition that made it send the
QoS events this setting exists alongside. The latency is the price of the
framerate, not an accident.

Decoding is not a constraint at this workload: nine feeds decode at ~200 fps
total with the CPU around 76% idle. Thermal throttling is real on a Pi 3
(`vcgencmd get_throttled` reports `0x80008` at 62-65 C) and worth addressing
for headroom, but it is not what limits framerate here.

`h264_freq=400` was tried, since the H.264 block idles at ~287MHz while the
core runs at 400MHz. It has no effect on this workload and was reverted. So was
raising `gpu_mem`: measured at 76MB and 256MB, total throughput was 216.6 and
212.3 fps respectively, so the stock allocation is fine and the extra 180MB is
better left to the system.

### Approaches considered and rejected

Recorded so they are not re-attempted:

- **A native atomic multi-plane presenter**, committing all nine planes in one
  atomic KMS commit instead of nine independent `kmssink` page flips. This was
  once believed mandatory, on the strength of an apparent "60/N page-flip
  ceiling". That measurement was taken with a starved 2-buffer output queue and
  the ceiling does not exist. With `qos=false` the sinks drop ~0% of what they
  receive, so an atomic presenter would optimise a path that is not lossy.
- **A Wayland compositor (Weston)** and **`glvideomixer`**. Both reintroduce
  the compositing step the design exists to avoid, and Weston caps out well
  below nine real feeds on this hardware.
- **Software HEVC decoding.** See the design constraints above.

The diagnostic that resolved the framerate question, worth reusing: the same
stream through a bare `gst-launch` pipeline decoded at a full 30 fps while
viewwall's graph gave 4 fps for the identical feed *running alone*. That ruled
out hardware, contention and the stream in one step, and pointed at something
the graph itself added; `GST_DEBUG=2,v4l2videodec:5` then named it outright
with "Dropping frame due to QoS". Asking "does it still fail with N=1?" is the
cheap question to ask first.

## Transport

Feeds request TCP, which is not the obvious choice: `rtspsrc` defaults to
offering every lower transport and letting the server pick, which it does in
the order UDP, UDP multicast, TCP. UDP is normally the cheaper option.

On the setup measured here it does not work. Nine UniFi Protect feeds on a Pi
3, 90 second runs of the real wall:

| `transport` | Result | Thread errors |
|---|---|---|
| `tcp` | survived 90s | 0 |
| `auto` | died after 6s | 1 |
| `udp` | died after 5s | 1 |
| `udp-mcast` | survived 90s, but no feed connected | 0 |

Both crashes look the same: GStreamer logs "failed to create thread: Error
creating thread: Resource temporarily unavailable" and GLib then aborts the
process with SIGTRAP. Under systemd `Restart=always` turns that into a restart
loop that never converges, which is why `auto` is unsuitable as a default here
despite being GStreamer's own.

`udp-mcast` survives only because nothing connects: no feed reaches healthy,
every viewport sits in backoff at 0 fps, and the log fills with "Could not read
from resource". This NVR publishes no multicast group, so no session is
established and none of the per-stream machinery is ever allocated. Judge that
row by the feeds, not by the exit code.

### What this does not establish

The measurement is one hardware and server combination, and the factors were
never varied against each other:

- **Whether the thread allocation is the cause or only where it surfaces.** A
  `TasksMax` ceiling is ruled out — it fails identically outside systemd, with
  127 threads on the machine against a limit of 8063 — but memory limits,
  `RLIMIT_STACK` and per-thread stack size were not examined.
- **Whether the Pi 3 or the NVR is the constraint.** Both were present in
  every run. A single feed over `udp` does work against this NVR, so the
  server serves UDP unicast and the problem appears with scale; where between
  one and nine it begins, and whether a different server behaves the same, is
  untested.
- **Whether any NVR publishes a multicast group.** Some RTSP servers do.
  `udp-mcast` is offered for that case; it simply has not been tried against
  one.

So the default is TCP because TCP is what has been shown to work at this
scale, not because UDP is known to be unusable in general.

## Latency

Framerate and latency are separate problems here; the wall runs at source rate
on every viewport while two latency effects remain.

### A constant ~250ms offset on one camera

One camera (a UniFi G6) sits about 252ms behind live while every other viewport is
around 65ms. It is constant rather than accumulating, and it runs at a full 30
fps.

**The cause is in the camera's SPS.** Comparing `sprop-parameter-sets`, first
four bytes:

```text
slow camera   67 4d 00 1e   profile_idc=77 constraint=0x00 level_idc=30
healthy       67 4d 40 1e   profile_idc=77 constraint=0x40 level_idc=30
```

Same profile (77, Main) and level (3.0), but the healthy cameras set
`constraint_set1_flag` (0x40), promising no frame reordering. The slow one sets
`0x00`, so `v4l2h264dec` cannot assume that and keeps a reorder buffer.
Measured decoding each feed alone, with no contention:

| | decode latency p50 | frames held in decoder |
|---|---|---|
| slow camera | 272.6 ms | 9 |
| healthy | 14.2 ms | 5 |

Eight extra frames at 30 fps is ~267ms, which matches the observed offset.

Ruled out by measurement: codec negotiation, timestamps (identical 33.3ms
spacing), B-frames (`pts == dts` on every stream), GOP length (150 frames
everywhere), frame size, jitterbuffer latency (50ms changed nothing), and
pre-decode arrival drift.

`v4l2h264dec` exposes no property to disable reordering, so the only fixes are
camera-side. Switching such a camera to H.265 would not help: the delay is a
reorder buffer, which an HEVC decoder would still honour.

### Observing it

The periodic metrics line reports `fps` (buffers reaching the plane),
`decoded_fps` (buffers leaving the decoder) and `queue_ms` (video waiting in
the output queue) per viewport, as structured journal fields. The pair of rates
separates the two failure modes that look alike on screen: frames never decoded
versus frames decoded and then dropped late. `queue_ms` is the direct read on
how far behind a viewport is.

A rotating viewport counts from its last switch rather than from the start of the
interval, so its `fps` describes the feed actually selected: a viewport alternating
a 3fps and a 24fps camera otherwise reported a meaningless ~13. Its decoder has
been running the whole interval, though, so `fps` and `decoded_fps` then cover
different spans; such a line carries `rotated=1` and the `window_s` it used,
because a ratio between them would read as frame loss that is not there.

Fields are attached to the record rather than formatted into it, so they can be
aggregated with `journalctl -o json` and `jq` without a parser. This is
deliberate: the numbers exist as floats in the runtime, and recovering them
from prose with a regex would break on any rewording.

### A sink that warns without losing frames

The one camera with a reorder buffer makes its sink log "a lot of buffers are
being dropped" roughly once a second, indefinitely. It is cosmetic: measured
over 44 one-minute samples that viewport averaged 29.86 fps against a 30 fps
source, with a worst minute of 27.5. The decoder holds frames and then releases
them together, and the sink reports each burst as an overrun.

The message comes from `GstBaseSink`, not from `kmssink`, and `qos=false` does
not silence it: that stops the QoS events, not the late-buffer counting behind
them. Viewwall logs this one message at debug level and every other GStreamer
warning at warning level. Left as a warning it was the only warning the wall
ever produced — 1279 in a day, all false — which is how a log channel stops
being read. The metrics measure the same thing honestly.

### Multi-second lag under decoder starvation

**Open.** With six competing decoders artificially starving the shared
`bcm2835-codec`, viewports fall about 5 seconds behind live. It recovers on its own
within roughly 15 seconds of the load lifting, and framerate degrades
gracefully to 20-21 fps, but the picture is badly stale while it lasts. This
has not been observed in normal operation.

The backlog is **upstream of every queue viewwall owns**: sampling occupancy
shows viewwall's own queues holding only 33-167ms throughout. `rtspsrc`'s
jitterbuffer is the likely place, since it has its own `latency` and
`drop-on-latency` and sits ahead of them. Next step is to instrument
`rtpjitterbuffer` occupancy directly.

A 300ms `max-size-time` on every video queue was tried as a fix for this and
removed: the queues never reach that depth, so the limit never fired.

## DRM plane discovery

By default Viewwall parses `kmsprint -l`, selects the connected output and
active mode, then chooses unused YUV-capable overlay planes compatible with
that CRTC.

The connector, CRTC, mode and planes are all taken from one block: `kmsprint`
nests a connector's CRTC beneath it, so reading them independently would
otherwise let a second display supply the mode while the connector still named
the first.

Which output that is depends on whether the configuration names one. With no
`[displays.<name>]` table at all, the first connected output is used. With a
table, its `connector_id` is required and decides, so the first connected
output is not a fallback for a named display -- pinning a connector that is
not connected is an error rather than a quiet move to another screen.

Planes are not configurable: a plane either drives the chosen CRTC, is free,
and can show YUV, or it cannot display a viewport at all, so there is nothing for
a user to choose between.

Every `kmssink` receives a duplicate of one coordinator-owned DRM file
descriptor. `skip-vsync=true` avoids independent sinks racing page flips on the
same CRTC. A future native atomic presenter could commit all planes together,
but is not required for the initial implementation.

### Driving more than one display

Each display is its own 0..1 canvas. A viewport cannot span two of them -- a KMS
plane belongs to exactly one CRTC -- so there is no shared coordinate space to
place viewports in, and `outer_margin_px` means the edge of that screen rather than
the edge of some union of screens. That also settles the seam question: a seam
is charged entirely to the viewport on its left, so one shared canvas would have a
viewport contribute a gap to a join that does not exist while its neighbour got
nothing along a real screen edge.

`[displays.<name>]` is one table per output, named in its header the way a
feed is, `[display_defaults]` supplies what they share, and a viewport names its
display. A named display must give a `connector_id`: naming one means saying
which, otherwise which screen shows what would depend on the order the probe
happened to list connectors in.

Driving one screen needs none of it. There is no display table to write, so
no name to invent and no connector to look up, and `[display_defaults]`
applies to the discovered display like any other -- setting a seam does not
mean naming a screen.

What stays in `[drm]` stays there for reasons stronger than convention:

- `device` names the DRM card, not the output. Both displays hang off the same
  card -- connectors are enumerated within it -- so a second value would mean
  a second GPU. It is also the process's one DRM file descriptor, duplicated
  into every `kmssink`, so a second device would be a second DRM master rather
  than a second setting.
- `poll_interval_seconds` paces a single timer whose probe already reports
  every connector, since `kmsprint -l` dumps the whole card. Per-display
  intervals would mean several timers each shelling out to that same command,
  which is the DRM contention the sysfs precheck exists to avoid.

#### Handing out planes

Planes are allocated across all displays at once rather than per display, and
not greedily. A plane can often drive several CRTCs -- sixteen of a Pi 3's
list `crtcs: 1 2 3` -- so a plane given to the first display may be the only
one left for the second while the first still had an exclusive plane to spare.
Serving displays in order therefore fails requests the hardware could satisfy,
and which requests fail depends on the order displays appear in the file.

It is a bipartite matching, so `_assign_planes` settles it with augmenting
paths: each demanded slot claims a plane, and on a collision the earlier
claimant is asked to move to another of its own candidates. A failure then
means the demand is genuinely unsatisfiable, not that the allocator gave up
early.

#### Detecting a mode change

The sysfs precheck reads each connector's `modes` and `connector_id`
attributes, so one cheap pass covers every display and a full `kmsprint` probe
runs only when something actually changed. A display whose mode changed is
re-laid-out on its own; the others are left alone.

Planes already handed out are kept across a mode change. They are bound to
sinks that are mid-flight, and a re-probe may order them differently, so
re-allocating would move a running viewport to a different plane for no reason.

#### Untested

None of this has run on hardware with two outputs. The Pi 3 B+ it was
developed on exposes four CRTCs and 33 planes but wires up a single HDMI port,
so the multi-display paths are covered by tests against captured and
synthesised `kmsprint` output only. Note also that the scaler budget is a
property of the HVS rather than of a CRTC: nine 1:1 planes is a total across
displays, not nine per display.

## SRTP

**SRTP is not supported, and a stream using it is stopped with a clear error.**
SRTP encrypts the RTP media packets, not just the control channel. `rtspsrc`
negotiates the session happily, but the depayloader cannot parse the encrypted
payload and fails with "The stream is in the wrong format" on the first buffer,
which the recovery logic would otherwise treat as a transient fault and retry
forever with nothing pointing at the cause.

Viewwall detects this from the **negotiated SDP**, not the request URI: the
RFC 4568 `a=crypto` key attribute (surfaced by `rtspsrc` as an `a-crypto` caps
field) and the `RTP/SAVP` profile are standard, whereas *how* a client asks for
SRTP is server-specific — UniFi Protect uses an `?enableSrtp` query parameter,
other servers differ. Such a feed is retired permanently rather than retried:

```text
feed <name>: server negotiated SRTP, which Viewwall cannot decrypt;
request an unencrypted-media stream instead
```

Other viewports are unaffected, and a viewport left with no healthy feed goes black
rather than holding a stale frame. URIs are always passed to `rtspsrc`
verbatim: no query parameter is inspected, rewritten, or stripped.

Supporting it is feasible but unimplemented. Protect advertises the key in the
SDP as RFC 4568 SDES, which `rtspsrc` surfaces on the stream caps but does not
act on, because its built-in SRTP support is MIKEY-oriented:

```text
a-crypto = "1 AES_CM_128_HMAC_SHA1_80 inline:<base64 key>"
```

The key arrives inside the TLS-protected control channel, and `srtpdec` is
available on this system, so the work is to read that caps field, base64-decode
the key, and supply it through `rtspsrc`'s `request-rtp-key` signal.
