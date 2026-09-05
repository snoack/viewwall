# Viewwall renders onto KMS overlay planes, so this image needs the host's DRM
# and V4L2 devices passed in. It cannot render anything in an ordinary
# sandboxed container. See the Docker section of README.md.
#
# Debian rather than a third-party Pi base, with the Raspberry Pi archive added
# for kms++-utils: viewwall shells out to kmsprint for KMS discovery and that
# package is not in the Debian archive.
FROM debian:trixie-slim

# The Raspberry Pi archive key carries a SHA1 binding signature, which trixie's
# default crypto policy rejects outright. Copy the keyring from the host's
# raspberrypi-archive-keyring package instead of relaxing verification.
COPY packaging/raspberrypi-archive-keyring.gpg /usr/share/keyrings/

RUN printf 'Types: deb\nURIs: http://archive.raspberrypi.com/debian/\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/raspberrypi-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/raspi.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-gi \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-tools \
        kms++-utils \
    && rm -rf /var/lib/apt/lists/*

COPY src/viewwall /usr/lib/python3/dist-packages/viewwall
COPY examples/viewwall.toml /usr/share/viewwall/viewwall.toml

RUN printf '#!/usr/bin/python3\nfrom viewwall.app import main\n\nif __name__ == "__main__":\n    main()\n' \
        > /usr/bin/viewwall \
    && chmod 0755 /usr/bin/viewwall

# The GStreamer registry must be writable, or the container rebuilds it on
# every start and logs a warning.
ENV GST_REGISTRY_1_0=/tmp/gst-registry.bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runs as root by default: DRM master and the numeric video/render group ids
# vary by host. Pass --user with --group-add to drop privileges (see README).
ENTRYPOINT ["/usr/bin/viewwall"]
CMD ["--config", "/etc/viewwall/viewwall.toml", "run"]
