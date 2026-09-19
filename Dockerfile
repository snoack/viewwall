# Viewwall renders onto KMS overlay planes, so this image needs the host's DRM
# and V4L2 devices passed in. It cannot render anything in an ordinary
# sandboxed container. See the Docker section of README.md.
#
# Debian rather than a third-party Pi base, with the Raspberry Pi archive added
# for kms++-utils: viewwall shells out to kmsprint for KMS discovery and that
# package is not in the Debian archive.
#
# The image installs the same .deb that a Pi installs, rather than copying the
# module and writing an entry point of its own. Those two descriptions of the
# package used to drift: gstreamer1.0-libav is only Recommends: in the .deb, so
# an image that repeated the dependency list by hand shipped with no H.264
# decoder at all, and every feed failed to start on a Pi 5 -- BCM2712 kept the
# HEVC block and dropped the H.264 one, where a Pi 3 hides the gap because
# v4l2h264dec comes from the kernel. Installing the package makes
# packaging/debian/control the only place dependencies are named.
# Pinned to the builder's own architecture rather than the target's. The
# package is Architecture: all, so building it under QEMU once per target
# platform would emulate an apt install and a shell script to produce the
# identical .deb. BUILDPLATFORM is set by buildx; a plain "docker build"
# without it falls back to the build host, which is the same thing.
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends dpkg-dev python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .
RUN scripts/build-deb.sh > /tmp/deb-path \
    && cp "$(cat /tmp/deb-path)" /tmp/viewwall.deb

FROM debian:trixie-slim

# The Raspberry Pi archive key carries a SHA1 binding signature, which trixie's
# default crypto policy rejects outright. Copy the keyring from the host's
# raspberrypi-archive-keyring package instead of relaxing verification.
COPY packaging/raspberrypi-archive-keyring.gpg /usr/share/keyrings/
COPY --from=build /tmp/viewwall.deb /tmp/viewwall.deb

# apt install rather than dpkg -i, so the package's own Depends are resolved
# rather than left for a second command. gstreamer1.0-libav is named
# explicitly instead of enabling recommends wholesale: it is the only
# Recommends: the image needs, and --install-recommends would pull them for
# every dependency in the tree as well.
RUN printf 'Types: deb\nURIs: http://archive.raspberrypi.com/debian/\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/raspberrypi-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/raspi.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/viewwall.deb gstreamer1.0-libav \
    && rm -f /tmp/viewwall.deb \
    && rm -rf /var/lib/apt/lists/*

# The GStreamer registry must be writable, or the container rebuilds it on
# every start and logs a warning.
ENV GST_REGISTRY_1_0=/tmp/gst-registry.bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runs as root by default: DRM master and the numeric video/render group ids
# vary by host. Pass --user with --group-add to drop privileges (see README).
ENTRYPOINT ["/usr/bin/viewwall"]
CMD ["--config", "/etc/viewwall/viewwall.toml", "run"]
