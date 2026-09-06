# Viewwall

Show all your security cameras on one screen, on a Raspberry Pi.

Viewwall turns a Pi and a TV into a camera wall: a grid of live RTSP feeds,
started at boot, that recovers on its own when a camera or the network drops
out. It draws straight to the screen through DRM/KMS, so there is no desktop,
no browser and no window manager.

Built and tested against **UniFi Protect**, and works with any camera or NVR
that publishes RTSP.

```text
+-------------+-------------+-------------+
|  driveway   |    porch    |   street    |
+-------------+-------------+-------------+
|    yard     |  doorbell   |   garage    |
+-------------+-------------+-------------+
|   shed      |   patio     |  side gate  |
+-------------+-------------+-------------+
```

- **Any layout.** Viewports are rectangles in fractions of the screen, so one
  configuration follows a change of resolution.
- **Hardware decoding, no copying.** Each feed is decoded once by the Pi's
  video hardware and scanned out on its own display plane. A Pi 3 sustains nine
  feeds at full camera framerate.
- **Rotation.** A viewport can cycle through several cameras, keeping them all
  connected so switching is instant.
- **Unattended.** Feeds reconnect on their own; systemd restarts the wall if it
  ever dies. It is meant to run for months on a shelf.

## Quick start

You need a Raspberry Pi 3 or newer running Raspberry Pi OS Lite, a screen,
and cameras that publish RTSP.

### 1. Install

Nothing needs configuring first: a current Raspberry Pi OS image works as it
comes.

Download the `.deb` from [releases](../../releases):

```sh
sudo apt install ./viewwall_0.1.0-1_all.deb
```

### 2. Describe the wall

```sh
sudoedit /etc/viewwall/viewwall.toml
```

```toml
[feeds.driveway]
uri = "rtsp://192.168.1.1:7447/aBcDeFgHiJkLmNoP"

[feeds.porch]
uri = "rtsp://192.168.1.1:7447/QrStUvWxYzAbCdEf"

# Each viewport shows a camera, placed as fractions of the screen.
[[viewports]]
x = 0
y = 0
width = "1/2"
height = 1
feeds = ["driveway"]

[[viewports]]
x = "1/2"
y = 0
width = "1/2"
height = 1
feeds = ["porch"]
```

On UniFi Protect, enable RTSP per camera in the Protect app. The URL it shows
needs two edits before it will play here:

```text
rtsps://192.168.1.1:7441/aBcDeFgHiJkLmNoP?enableSrtp   <- what Protect shows
rtsp://192.168.1.1:7447/aBcDeFgHiJkLmNoP               <- what to configure
```

Drop `?enableSrtp`, which asks for encrypted media Viewwall cannot decrypt, and
switch `rtsps://` on 7441 to `rtsp://` on 7447. The stream id stays as it is,
and there are no credentials. To keep TLS instead, leave the `rtsps://` URL on
7441 and see [RTSP and RTSPS](#rtsp-and-rtsps) -- but still drop `?enableSrtp`.

Other cameras that want a login take it inline as `rtsp://user:password@host/path`.

### 3. Check the config

```sh
sudo -u viewwall viewwall -c /etc/viewwall/viewwall.toml validate
```

### 4. Start it

```sh
sudo systemctl enable --now viewwall
```

The wall appears within a few seconds. To watch what it is doing:

```sh
journalctl -u viewwall -f
```

## Configuration

Anything omitted uses its default, so a working configuration is mostly feeds
and viewports. A fuller example is in
[examples/viewwall.toml](examples/viewwall.toml). An option Viewwall does not
recognise is an error rather than a silent no-op, so a typo says so instead of
leaving a setting at its default.

**`[[viewports]]`** — one table per viewport: a region of the screen showing
one camera. Viewports have no names; logs and metrics identify one by its
position in the file, counting from 1, and name the feed it is showing
alongside.

| Option | Default | Meaning |
|---|---|---|
| `x`, `y` | required | Where the viewport's top-left corner sits, as a fraction of the screen: `"1/3"`, or a whole `0` or `1`. |
| `width`, `height` | required | How much of the screen it covers, in the same units. A 3x3 wall is nine viewports of `1/3`. The camera is stretched to fill it, deliberately: it avoids black bars around a doorbell camera that is not 16:9. |
| `feeds` | required | Feed names. Listing more than one rotates the viewport through them; every one stays connected and decoding, so a switch is instant rather than waiting for the stream to start, and unhealthy cameras are skipped. |
| `rotate_seconds` | `8` | Seconds between switches; must be positive. Ignored on a single-feed viewport. |
| `display` | the only display | Which display the viewport is on, by `name`. Only needed with more than one `[displays.<name>]` table, since otherwise there is a single display to be on: the one configured, or the first one connected when none is. |

**`[feeds.<name>]`** — one table per camera.

| Option | Default | Meaning |
|---|---|---|
| `uri` | required | `rtsp://` or `rtsps://`. A `${VAR}` placeholder is expanded from the environment. H.264 or H.265 is detected from the RTSP announcement. |
| `latency_ms` | `150` | Jitterbuffer depth. Raise it on a lossy network, at the cost of latency. |
| `transport` | `"tcp"` | The RTP transport: `"tcp"`, `"udp"`, `"udp-mcast"`, or `"auto"` to negotiate one of the three with the server. TCP (default) was the only one to work reliably during testing using a Raspberry Pi 3 and UniFi NVR; `udp` and `auto` crashed within seconds and `udp-mcast` connected to nothing. |
| `verify_tls` | `true` | `rtsps://` only. `false` accepts a self-signed certificate; the transport stays encrypted, the server is simply not verified. |

**`[feed_defaults]`** — `latency_ms`, `transport` and `verify_tls` for any feed
that does not set its own.

**`[displays.<name>]`** — one table per output, needed only to drive more than
one screen. With none of these, the first connected output is used and the
rest are left alone; `[display_defaults]` still applies to it, so setting a
seam does not mean naming a screen.

| Option | Default | Meaning |
|---|---|---|
| `connector_id` | required | Which output this is. `kmsprint -l` lists them as `Connector 0 (35) HDMI-A-1 (connected)`, where `35` is the id, and the startup log line names the ones in use. Naming a display means saying which one: otherwise which screen showed what would depend on probe order. |
| `gap_px` | `0` | Seam between viewports, in pixels. Made by trimming that pixel off the edge of the video rather than drawing over it, so the picture is never scaled and every plane stays on the cheap 1:1 path. |
| `outer_margin_px` | `0` | Inset from this screen's edges. |
| `width`, `height` | active mode | Overrides the detected resolution. Both or neither. |

**`[display_defaults]`** — `gap_px` and `outer_margin_px` for any display that
does not set its own, including the discovered one.

Each display is its own canvas: a viewport's position is a fraction of the
screen it is on, not of some union of screens. A viewport cannot span two displays, because
a KMS plane belongs to exactly one output.

```toml
[displays.hall]
connector_id = 35

[displays.office]
connector_id = 36
gap_px = 1

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["driveway"]
display = "hall"

[[viewports]]
x = 0
y = 0
width = "1/2"
height = "1/2"
feeds = ["porch"]
display = "office"
```

A feed may appear on both, and is still decoded once. The planes each display
needs are worked out together, since on a Pi many planes can drive more than
one output and a plane handed to one display is not available to the other.

**`[metrics]`**

| Option | Default | Meaning |
|---|---|---|
| `interval_seconds` | `60` | How often to log per-viewport framerate and queue depth. `0` disables. |

**`[drm]`** — the graphics card, which is one card however many displays.

| Option | Default | Meaning |
|---|---|---|
| `device` | `"/dev/dri/card0"` | DRM card to open. Displays are connectors within one card, not separate devices. |
| `poll_interval_seconds` | `2` | How often to re-probe the card for a resolution change. One probe reports every connector. |
| `background` | `"#000000"` | Color painted under the viewports as `#RRGGBB` or `"none"` to leave the framebuffer console showing in the background. |

### Metrics

Every 60 seconds viewwall logs what each viewport is doing:

```text
metrics viewport=2 feed=porch state=healthy queued_fps=29.6 decoded_fps=29.8 \
  window_s=60.0 presented_fps=7.4 sink_dropped=1327 queue_ms=45
```

The three rates follow one frame along the path, and a drop between any two
names where it is being lost:

| Field | Where it is counted |
|---|---|
| `decoded_fps` | Frames leaving the decoder. |
| `queued_fps` | Buffers leaving the viewport's output queue toward its plane. A gap below `decoded_fps` is the leaky queue shedding frames the wall could not keep up with. |
| `presented_fps` | Buffers the sink actually put on the KMS plane. This is what reaches the screen. |
| `sink_dropped` | Buffers the sink discarded for arriving late against the clock, which is the difference between the two rates above. |

Expect `presented_fps` to sit well below `queued_fps` on a full wall. Each
plane is updated by its own `drmModeSetPlane` and the VC4 retires one per
vblank, so the viewports share 60 updates a second between them: a nine-viewport
wall measures about 7.4fps each, summing to 60. See the plane budget section of
DESIGN.md.

`queue_ms` is how much video is waiting — a viewport sitting at 250ms is a
quarter of a second behind live.

A rotating viewport that switched feeds during the interval is marked `rotated=1`,
with `window_s` giving the shorter span the rates cover. Its rates and
`decoded_fps` are measured over different spans and should not be compared.

Under systemd these are attached as structured journal fields, so they can be
summarized without parsing the text:

```sh
journalctl -u viewwall -o json --output-fields=VW_VIEWPORT,VW_QUEUED_FPS,VW_PRESENTED_FPS \
  | jq -r 'select(.VW_VIEWPORT) | [.VW_VIEWPORT, .VW_QUEUED_FPS, .VW_PRESENTED_FPS] | @tsv'
```

To change the interval, or turn it off:

```toml
[metrics]
interval_seconds = 60   # 0 disables
```

### RTSP and RTSPS

Feeds use `rtsp://` over TCP by default (`feed_defaults.transport = "tcp"`).

`rtsps://` works. A UniFi Protect NVR presents a self-signed certificate, so
certificate validation has to be relaxed:

```toml
[feeds.backyard]
uri = "rtsps://192.168.1.1:7441/aBcDeFgHiJkLmNoP"
verify_tls = false
```

`verify_tls` may be set per feed or in `[feed_defaults]`, and applies only to
`rtsps://` URIs. It is a boolean rather than a set of modes: a partial
validation mask looks like the right way to accept just a self-signed
certificate, but
GLib deprecated per-condition flags and this GStreamer treats any non-zero value
as full validation. Measured against a Protect NVR, flags 122/126/127 all fail
with "Could not open resource for reading and writing" and only 0 connects, so
a partial mode would silently do nothing. With `verify_tls = false` the
transport is still encrypted; the server is simply not verified. Viewwall logs
a warning whenever a feed runs this way.

Protect offers both per camera: 7447 for plain RTSP and 7441 for RTSPS. The
samples here use 7447, because on a LAN it is the better trade. RTSPS does not
improve image quality or framerate, costs a little extra CPU for TLS on a Pi
that has none to spare, and against a self-signed certificate it buys less than
it appears to: `verify_tls = false` stops passive sniffing but not an active
attacker, who can present their own certificate and be believed. That is close
to the plain-RTSP threat model with extra steps.

RTSPS earns its keep when the feeds cross a network you do not control, or when
the server has a certificate you can actually verify -- with `verify_tls` left
at `true`, the guarantee is real.

SRTP is **not supported**, and Protect asks for it by default: the URL in the
app carries `?enableSrtp`, which has to come off. Left on, the stream connects
and then fails because the payload cannot be decrypted -- Viewwall detects this
from the negotiated SDP and stops that feed with a clear message rather than
retrying forever.

Removing the parameter is the only change needed. SRTP encrypts the media
payload, which is separate from whether the control channel uses TLS, so
`rtsps://` on 7441 works fine without it. See [DESIGN.md](DESIGN.md) for what
implementing SRTP would involve.

## Running in Docker

The wall writes directly to KMS overlay planes, so the container needs the
host's DRM and V4L2 devices. It cannot render anything in an ordinary sandboxed
container, and nothing else may hold DRM master — stop a native `viewwall`
service before starting the container.

```yaml
# compose.yaml
services:
  viewwall:
    image: ghcr.io/<owner>/viewwall:0.1.0
    restart: unless-stopped
    devices:
      - /dev/dri/card0:/dev/dri/card0   # KMS output planes
      - /dev/video10:/dev/video10       # hardware H.264 decoder
    group_add:
      # Numeric GIDs, not names: group names are resolved inside the container,
      # whose /etc/group has no video or render entry. Check yours with
      #   getent group video render
      - "44"    # video
      - "992"   # render
    volumes:
      - /etc/viewwall:/etc/viewwall:ro
    environment:
      # A "${PORCH_RTSP}" placeholder in viewwall.toml is expanded from the
      # environment, which keeps the credentials out of the config file and
      # lets Compose supply them.
      PORCH_RTSP: ${PORCH_RTSP}
```

Stop any native service first, or the container cannot take DRM master:

```sh
sudo systemctl stop viewwall
docker compose up -d
```

Verified on a Raspberry Pi 3 running Raspberry Pi OS Trixie: the container
renders all nine planes from live cameras with exactly the two device nodes and
two groups above.

Pass every `/dev/videoN` decoder node your Pi exposes if you are unsure which
one GStreamer will pick; on a Pi 3 the H.264 decoder is `/dev/video10`.

A native systemd install is lighter than Docker on a Pi 3, and is the
recommended deployment.

## Supported hardware

**Raspberry Pi 3, running Raspberry Pi OS.** Developed and verified on a Pi 3
Model B+ with Raspberry Pi OS Trixie. Pi 4 and Pi 5 should work but are
untested. There is little reason to reach for one: nine feeds leave the Pi 3's
CPU around 76% idle, and the real ceiling is the VC4 scaler budget, which a
newer model does not obviously lift.

Older Pi models are not supported: they lack the KMS overlay planes the design
depends on.

Other hardware, including x86, is out of scope for now. The design itself is
not Pi-specific: the layout model, recovery logic, GStreamer graph and KMS
output path would all port. Three things currently tie it to a Pi:

- `kms++-utils`, used for KMS discovery, is packaged by Raspberry Pi OS rather
  than Debian, so the `.deb` cannot satisfy its dependencies on plain x86
  Debian. The published image is built for `linux/arm/v7` and `linux/arm64`.
- Decoder selection prefers the Pi's V4L2 M2M decoders. H.264 falls back to
  software `avdec_h264`, so it would run on x86 but on the CPU; there is no
  VAAPI or NVDEC path.
- A wall needs one overlay plane per viewport. Intel and AMD display engines
  typically expose far fewer than the nine a 3x3 wall wants.

Supporting x86 properly would mean replacing the `kmsprint` shell-out with
direct DRM queries and adding VAAPI to the decoder policy.

## Troubleshooting

**Nothing appears on screen.** Check `journalctl -u viewwall -n 50`. "No
unused YUV-capable KMS overlay planes found" means the display driver is not
full KMS. Raspberry Pi OS enables it by default, so this points at a
`dtoverlay` line in `/boot/firmware/config.txt` selecting the legacy or
`vc4-fkms-v3d` driver; neither is supported.

**One viewport is black.** That camera is unreachable or still starting. Viewwall
retries on its own, backing off up to 30 seconds; the log names the feed.

**Every viewport is black.** Viewwall logs `no viewport is showing video` at error
level and reports it to systemd, so `systemctl status viewwall` shows it too.
That is the NVR being unreachable, credentials having changed, or a
`transport` the streams cannot use.

**"A lot of buffers are being dropped" at `--log-level DEBUG`.** GStreamer
emits this from a late-buffer count alone, and a camera whose decoder holds
and then releases frames in bursts trips it continuously without losing any.
Check that viewport's `fps` in the metrics rather than trusting the warning; it is
logged at debug level for this reason.

**A viewport is behind the others.** Check `queue_ms` in the metrics lines.
Some cameras declare frame reordering in
their H.264 stream, which makes the decoder hold frames and puts that viewport a
quarter-second behind the rest. It is a camera-side setting; see
[DESIGN.md](DESIGN.md).

**Feeds fail to start.** The log names the feed and the reason. A camera that
is unreachable is retried; one that keeps failing usually points at the URL,
credentials, or a codec viewwall cannot decode on that model.

**It worked, then stopped after adding cameras.** A wall needs one display
plane per viewport, and hardware runs out of them. The log reports a resource
error naming the viewport that failed.

## How it works

[DESIGN.md](DESIGN.md) covers the media path, the recovery model, how viewports are
resolved to pixels, the measurements behind the current settings, and the
approaches that were tried and rejected.

## Development

Unit tests cover configuration validation, fractional layout resolution,
source-crop calculation, and KMS discovery parsing:

```sh
pytest -q
```

Live RTSP, V4L2 decoder, DMA-BUF, and KMS behavior must be tested on the Pi.

## Building the artifacts

```sh
./scripts/build-deb.sh          # -> dist/viewwall_<version>-<rev>_all.deb
docker build -t viewwall:dev .  # needs an armhf/arm64 builder for a Pi
```

To cut a release, set the version in `pyproject.toml`, commit, then tag:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

That runs the release workflow, which tests, builds the package, pushes a
multi-arch image to GHCR (`{{version}}`, `{{major}}.{{minor}}` and `latest`),
and creates a GitHub release with the `.deb` attached. The version comes from
`pyproject.toml` rather than from the tag; the workflow compares the two and
fails if they disagree.

The `.deb` is `Architecture: all` — it is pure Python, a unit file and a
wrapper script, so one package serves every supported Pi. The Pi-specific part
is its dependencies, not its contents.

**After the first release only**, make the GHCR package public, or pulls fail
with a 403: a package pushed by `GITHUB_TOKEN` is private by default, and its
visibility can only be set once the package exists. Find it under the
repository's Packages, then Package settings → Change visibility. This is a
one-time step and is independent of whether the repository itself is public.

## License

GPL-3.0-or-later. Copyright Sebastian Noack.
