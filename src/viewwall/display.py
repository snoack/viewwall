from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import ctypes
import ctypes.util
import fcntl
import functools
import logging
import mmap
import os
import re
import struct
import subprocess

from .config import DisplayConfig

LOG = logging.getLogger(__name__)

class DisplayError(RuntimeError):
    """Raised when no usable active KMS output can be identified."""


@dataclass(frozen=True)
class DisplayState:
    connector_id: int
    crtc_index: int
    crtc_id: int
    width: int
    height: int
    plane_ids: tuple[int, ...]


_CONNECTOR_RE = re.compile(r"^Connector\s+\d+\s+\((\d+)\)\s+\S+\s+\(connected\)")
_CRTC_RE = re.compile(r"^Crtc\s+(\d+)\s+\((\d+)\)\s+(\d+)x(\d+)@")
_PLANE_RE = re.compile(
    r"^Plane\s+\d+\s+\((\d+)\)(.*)\(crtcs:\s*([^)]+)\)(.*)$"
)


def parse_kmsprint_all(text: str) -> list[DisplayState]:
    """Read every connected output from "kmsprint -l", in listed order.

    kmsprint nests a connector's CRTC beneath it, so the two are taken from
    the same block: tracking them independently let a second display supply
    the CRTC and mode while the connector still named the first, which pairs a
    connector with planes belonging to another CRTC.

    Planes are flat rather than nested, each listing the CRTCs it can drive,
    so they are collected once and matched to each connector afterwards. A
    plane that can drive several CRTCs is offered to each of them; assigning
    it to one display is the caller's job.
    """
    connectors: list[tuple[int, int, int, int, int]] = []
    plane_rows: list[tuple[int, str, set[int]]] = []
    pending: int | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        connector_match = _CONNECTOR_RE.match(line)
        if connector_match:
            pending = int(connector_match.group(1))
            continue
        crtc_match = _CRTC_RE.match(line)
        if crtc_match:
            if pending is not None:
                connectors.append(
                    (
                        pending,
                        int(crtc_match.group(1)),
                        int(crtc_match.group(2)),
                        int(crtc_match.group(3)),
                        int(crtc_match.group(4)),
                    )
                )
                pending = None
            continue
        plane_match = _PLANE_RE.match(line)
        if plane_match:
            supported = {int(item) for item in plane_match.group(3).split()}
            details = plane_match.group(2) + plane_match.group(4)
            plane_rows.append((int(plane_match.group(1)), details, supported))

    states: list[DisplayState] = []
    for connector_id, crtc_index, crtc_id, width, height in connectors:
        planes = tuple(
            plane_id
            for plane_id, details, supported in plane_rows
            if crtc_index in supported
            and "fb-id:" not in details
            and ("YU12" in details or "NV12" in details or "YV12" in details)
        )
        states.append(
            DisplayState(
                connector_id=connector_id,
                crtc_index=crtc_index,
                crtc_id=crtc_id,
                width=width,
                height=height,
                plane_ids=planes,
            )
        )
    return states


_SYSFS_DRM = Path("/sys/class/drm")
_MODE_RE = re.compile(r"^(\d+)x(\d+)")
_DEFAULT_CARD = "/dev/dri/card0"


def available_modes(
    connector_id: int, sysfs_root: Path = _SYSFS_DRM
) -> set[tuple[int, int]]:
    """Every mode a connector advertises, from sysfs.

    Used to reject a configured mode before the wall is built. Setting one the
    display cannot show otherwise fails deep in kmssink as "Internal data
    stream error", which names neither the mode nor the option that chose it.

    An empty set means sysfs said nothing, not that nothing is supported, so
    callers must treat it as unknown rather than as a rejection.
    """
    modes: set[tuple[int, int]] = set()
    try:
        connectors = sorted(sysfs_root.glob("card*-*"))
    except OSError:
        return modes
    for connector in connectors:
        try:
            if int((connector / "connector_id").read_text().strip()) != connector_id:
                continue
            lines = (connector / "modes").read_text().splitlines()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for line in lines:
            match = _MODE_RE.match(line.strip())
            if match:
                modes.add((int(match.group(1)), int(match.group(2))))
    return modes


def current_modes(sysfs_root: Path = _SYSFS_DRM) -> dict[int, tuple[int, int]]:
    """Active mode per connector id, read from sysfs.

    kmsprint has to open the DRM device, which contends with the wall's own
    page flips: measured on a Pi 3 it takes 0.1s with the wall stopped and a
    median of 7s while nine planes are scanning out, so a 5s timeout fails
    most of the time. sysfs is a plain file read costing under 50ms under the
    same load, which is enough to notice that the resolution changed.

    sysfs names connectors by type and index rather than by DRM id, so the id
    is read from each connector's own attribute where the kernel exposes it;
    connectors that do not are skipped, and the caller falls back to a full
    probe rather than assuming anything.
    """
    modes: dict[int, tuple[int, int]] = {}
    try:
        connectors = sorted(sysfs_root.glob("card*-*"))
    except OSError:
        return modes
    for connector in connectors:
        try:
            if (connector / "status").read_text().strip() != "connected":
                continue
            first = (connector / "modes").read_text().split("\n", 1)[0]
            connector_id = int((connector / "connector_id").read_text().strip())
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        match = _MODE_RE.match(first.strip())
        if match:
            modes[connector_id] = (int(match.group(1)), int(match.group(2)))
    return modes


def detect_card(sysfs_root: Path = _SYSFS_DRM) -> str:
    """Name the DRM card whose connectors are the display outputs.

    A Pi 3 has one card and it is card0, which is why that was the default for
    so long. A Pi 5 has two: card0 is v3d, the render-only GPU with no
    connectors at all, and card1 is vc4-drm, which owns both HDMI outputs.
    Opening card0 there gives a card that can never scan out, and the failure
    surfaces later as kmssink refusing a plane rather than as a bad device.

    Connectors decide it rather than the driver name: sysfs nests each
    connector under its card as "card1-HDMI-A-1", so a card that has one is a
    card that can drive a screen. A connected connector wins over a merely
    present one, so a second card with nothing plugged in does not take
    precedence over the one showing a picture -- but a card with connectors
    and nothing attached still beats a render node, which keeps an unplugged
    display reporting "no connected KMS connector found" as it always did
    instead of naming the wrong card.
    """
    with_connected: list[str] = []
    with_connectors: list[str] = []
    for entry in sorted(sysfs_root.glob("card[0-9]*")):
        if "-" in entry.name:
            # A connector ("card1-HDMI-A-1"), not a card.
            continue
        connectors = sorted(sysfs_root.glob(f"{entry.name}-*"))
        if not connectors:
            # A render-only node such as the Pi 5's v3d.
            continue
        with_connectors.append(entry.name)
        for connector in connectors:
            try:
                status = (connector / "status").read_text().strip()
            except (OSError, UnicodeDecodeError):
                continue
            if status == "connected":
                with_connected.append(entry.name)
                break
    for candidates in (with_connected, with_connectors):
        if candidates:
            return f"/dev/dri/{candidates[0]}"
    return _DEFAULT_CARD


def _run_kmsprint() -> str:
    try:
        result = subprocess.run(
            ["kmsprint", "-l"],
            check=True,
            capture_output=True,
            text=True,
            # Generous, because kmsprint contends with the wall's own page
            # flips; see current_modes(). Callers polling for a mode change
            # should use that instead of calling this repeatedly.
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DisplayError(f"kmsprint failed: {exc}") from exc
    return result.stdout


def detect_displays(
    configs: Sequence[DisplayConfig], plane_demand: Mapping[str, int] | None = None
) -> dict[str, DisplayState]:
    """Resolve every configured display from one kmsprint run.

    One run, not one per display: kmsprint opens the DRM device and contends
    with the wall's own page flips, so probing per display would multiply the
    cost that current_modes() exists to avoid.

    Planes are then handed out. A plane can often drive several CRTCs -- on a
    Pi 3, sixteen of them list "crtcs: 1 2 3" -- so a plane offered to two
    displays must go to exactly one of them or two kmssinks would fight over
    it. Displays are served in configured order, and "plane_demand" says how
    many each needs, so a display asking for two does not lose a shared plane
    to one that asks for none.
    """
    text = _run_kmsprint()
    states = parse_kmsprint_all(text)
    if not states:
        raise DisplayError("no connected KMS connector found")
    by_connector = {state.connector_id: state for state in states}

    resolved: dict[str, DisplayState] = {}
    chosen: dict[str, DisplayState] = {}
    used_connectors: set[int] = set()
    for config in configs:
        if config.connector_id is None:
            # A single display may omit the connector, whether or not it has
            # a table: with one of them there is nothing to disambiguate.
            # Other screens may well be attached: the first connected one is
            # used and the rest are left alone, which is what driving them
            # requires a table of their own to say.
            state = states[0]
        else:
            state = by_connector.get(config.connector_id)
            if state is None:
                raise DisplayError(
                    f"display {config.name}: connector_id "
                    f"{config.connector_id} is not a connected KMS connector"
                )
        if state.connector_id in used_connectors:
            raise DisplayError(
                f"display {config.name}: connector {state.connector_id} is "
                "already driven by another display"
            )
        used_connectors.add(state.connector_id)
        chosen[config.name] = state

    demand = {
        config.name: (
            plane_demand.get(config.name, len(chosen[config.name].plane_ids))
            if plane_demand is not None
            else len(chosen[config.name].plane_ids)
        )
        for config in configs
    }
    for config in configs:
        # Before allocation, not after: with no candidate planes at all
        # _assign_planes fails first, and its message is about arbitrating
        # between displays. Overlay planes are exactly what the legacy and
        # fkms display drivers do not provide, so name that instead -- it is
        # the likeliest cause by far.
        if demand[config.name] > 0 and not chosen[config.name].plane_ids:
            raise DisplayError(
                "no unused YUV-capable KMS overlay planes found; the display "
                "driver is probably not full KMS"
            )
    assignment = _assign_planes(configs, chosen, demand)

    for config in configs:
        state = chosen[config.name]
        planes = assignment[config.name]
        resolved[config.name] = DisplayState(
            connector_id=state.connector_id,
            crtc_index=state.crtc_index,
            crtc_id=state.crtc_id,
            width=config.width or state.width,
            height=config.height or state.height,
            plane_ids=planes,
        )
    return resolved


def _assign_planes(
    configs: Sequence[DisplayConfig],
    chosen: Mapping[str, DisplayState],
    demand: Mapping[str, int],
) -> dict[str, tuple[int, ...]]:
    """Give each display the planes it needs, with no plane used twice.

    Handing them out greedily is wrong: a plane that can drive several CRTCs
    -- sixteen of a Pi 3's list "crtcs: 1 2 3" -- may be the only one left for
    a later display while an earlier one still had an exclusive plane to
    spare. That fails a request the hardware could have satisfied, and the
    failure would depend on the order displays happen to be configured in.

    This is bipartite matching, so augmenting paths settle it: each demanded
    slot claims a plane, and on a collision the earlier claimant is asked to
    move to another of its own candidates.
    """
    slots: list[tuple[str, list[int]]] = []
    for config in configs:
        candidates = list(chosen[config.name].plane_ids)
        for _ in range(demand[config.name]):
            slots.append((config.name, candidates))

    owner: dict[int, int] = {}

    def claim(slot: int, seen: set[int]) -> bool:
        for plane_id in slots[slot][1]:
            if plane_id in seen:
                continue
            seen.add(plane_id)
            held_by = owner.get(plane_id)
            if held_by is None or claim(held_by, seen):
                owner[plane_id] = slot
                return True
        return False

    for index in range(len(slots)):
        if not claim(index, set()):
            name = slots[index][0]
            state = chosen[name]
            raise DisplayError(
                f"display {name}: need {demand[name]} KMS overlay planes on "
                f"connector {state.connector_id}, but they cannot all be "
                "satisfied alongside the other displays"
            )

    assignment: dict[str, list[int]] = {config.name: [] for config in configs}
    for plane_id, slot in owner.items():
        assignment[slots[slot][0]].append(plane_id)
    return {
        name: tuple(sorted(planes)) for name, planes in assignment.items()
    }


# --- mode setting -----------------------------------------------------------
#
# The one place the wall calls libdrm directly, because kmssink cannot express
# a refresh rate: it matches a mode by resolution alone, so a panel offering
# 1920x1080 at 120, 60, 50, 30 and 24 gives whichever the driver resolves
# first -- 120 on the measured panel, the worst of them for a compositor
# blending nine tiles every output frame.
#
# Called once at startup on the fd the runtime already holds, which is what
# makes it stick: the kernel restores the previous mode when the fd that set
# it closes, so a mode set on a borrowed descriptor would last only as long
# as the call.
#
# The structs mirror libdrm's and are ABI-sensitive -- a mismatched layout
# reads nonsense rather than failing -- so set_crtc_mode() sanity-checks what
# it reads back before trusting any of it.


class _DrmModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16),
        ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16),
        ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16),
        ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16),
        ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
    ]


class _DrmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint),
        ("mmWidth", ctypes.c_uint32),
        ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint),
        ("count_modes", ctypes.c_int),
        ("modes", ctypes.POINTER(_DrmModeInfo)),
        ("count_props", ctypes.c_int),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class _DrmModeCrtc(ctypes.Structure):
    _fields_ = [
        ("crtc_id", ctypes.c_uint32),
        ("buffer_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("mode_valid", ctypes.c_int),
        ("mode", _DrmModeInfo),
        ("gamma_size", ctypes.c_int),
    ]


@functools.lru_cache(maxsize=1)
def _libdrm() -> ctypes.CDLL | None:
    """libdrm, or None when it cannot be loaded.

    Never fatal. A wall that came up at the connector's default refresh rate
    is what every release before this one shipped, and it beats no wall.

    Cached because find_library() searches the linker cache -- one call per
    configured display otherwise, and the None answer is worth remembering
    too so a box without libdrm does not repeat the search.
    """
    path = ctypes.util.find_library("drm")
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(path, use_errno=True)
    except OSError:
        return None
    lib.drmModeGetConnector.restype = ctypes.POINTER(_DrmModeConnector)
    lib.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
    lib.drmModeFreeConnector.argtypes = [ctypes.POINTER(_DrmModeConnector)]
    lib.drmModeGetCrtc.restype = ctypes.POINTER(_DrmModeCrtc)
    lib.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
    lib.drmModeFreeCrtc.argtypes = [ctypes.POINTER(_DrmModeCrtc)]
    lib.drmModeAddFB2.restype = ctypes.c_int
    lib.drmModeAddFB2.argtypes = [
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32 * 4,
        ctypes.c_uint32 * 4,
        ctypes.c_uint32 * 4,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
    ]
    lib.drmModeSetCrtc.restype = ctypes.c_int
    lib.drmModeSetCrtc.argtypes = [
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int,
        ctypes.POINTER(_DrmModeInfo),
    ]
    return lib


# DRM_IOCTL_MODE_CREATE_DUMB / MAP_DUMB, from drm_mode.h. Encoded here rather
# than derived: _IOWR('d', 0xB2, struct drm_mode_create_dumb) is stable ABI,
# and computing it would mean reproducing the _IOC macros for one constant.
_DRM_IOCTL_MODE_CREATE_DUMB = 0xC02064B2
_DRM_IOCTL_MODE_MAP_DUMB = 0xC01064B3
_DRM_FORMAT_XRGB8888 = 0x34325258  # 'XR24'


class _BackgroundBuffer:
    """A solid-colour framebuffer the CRTC can scan out on its own.

    Held for the life of the process: the kernel drops the mode when the
    framebuffer goes away, the same way it drops it when the fd closes.
    """

    def __init__(self, fb_id: int, handle: int) -> None:
        self.fb_id = fb_id
        self.handle = handle
        # Set by the caller: which configured display this belongs to.
        self.display: str | None = None


def _create_background_fb(
    lib, fd: int, width: int, height: int, colour: int
) -> _BackgroundBuffer | None:
    """Allocate a dumb buffer, fill it with one colour, register it as an FB.

    Returns None on any failure: a wall with a visible console is worse than
    no wall, so every step here degrades to letting the caller carry on.
    """
    # struct drm_mode_create_dumb: height, width, bpp, flags, handle, pitch, size
    create = bytearray(struct.pack("IIIIIIQ", height, width, 32, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, _DRM_IOCTL_MODE_CREATE_DUMB, create)
    except OSError as exc:
        LOG.warning("could not allocate a background buffer: %s", exc)
        return None
    _h, _w, _bpp, _flags, handle, pitch, size = struct.unpack("IIIIIIQ", create)

    # struct drm_mode_map_dumb: handle, pad, offset
    mapping = bytearray(struct.pack("IIQ", handle, 0, 0))
    try:
        fcntl.ioctl(fd, _DRM_IOCTL_MODE_MAP_DUMB, mapping)
        _handle, _pad, offset = struct.unpack("IIQ", mapping)
        with mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE,
                       offset=offset) as buf:
            # XR24 is little-endian BGRX in memory, so one 32-bit word
            # repeated fills every pixel of every line.
            buf[:] = struct.pack("<I", colour) * (size // 4)
    except (OSError, ValueError) as exc:
        LOG.warning("could not paint the background buffer: %s", exc)
        return None

    handles = (ctypes.c_uint32 * 4)(handle, 0, 0, 0)
    pitches = (ctypes.c_uint32 * 4)(pitch, 0, 0, 0)
    offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
    fb_id = ctypes.c_uint32()
    result = lib.drmModeAddFB2(
        fd, width, height, _DRM_FORMAT_XRGB8888,
        handles, pitches, offsets, ctypes.byref(fb_id), 0,
    )
    if result != 0:
        LOG.warning(
            "could not register the background buffer: %s",
            os.strerror(ctypes.get_errno()),
        )
        return None
    return _BackgroundBuffer(fb_id.value, handle)


def set_crtc_mode(
    fd: int,
    connector_id: int,
    crtc_id: int,
    width: int,
    height: int,
    refresh: int,
    background: int | None = None,
) -> _BackgroundBuffer | None:
    """Drive a connector at an exact refresh rate, and own what it scans out.

    "fd" is the runtime's own DRM descriptor rather than one opened here: the
    kernel reverts the mode when the descriptor that set it closes, so a mode
    set on a borrowed fd would last only as long as this call.

    With a background colour this allocates its own framebuffer and passes it
    to drmModeSetCrtc, which puts it on the CRTC's primary plane -- the
    scanout surface, not one of the overlays the viewports compete for, so it
    costs none of their budget. That is what lets kmssink stop modesetting:
    it matches a mode on width and height alone and takes the first hit, so
    on a panel listing 1920x1080@120 before @60 its modeset silently replaced
    the rate this function had just set. Owning the scanout buffer removes
    its reason to modeset at all.

    Without one the framebuffer already on the CRTC is reused. vc4 refuses
    drmModeSetCrtc with fb_id 0 (ENOENT), and whatever lit the CRTC -- the
    framebuffer console, in the measured case -- is a good enough buffer to
    keep scanning out under the overlays. A CRTC with no framebuffer and no
    colour to paint is left alone.

    Returns the buffer it allocated, which the caller has to keep: the kernel
    drops the mode when the framebuffer is released. None when it allocated
    none, whether or not the mode changed.
    """
    lib = _libdrm()
    if lib is None:
        LOG.warning("libdrm is unavailable; leaving the mode alone")
        return None

    connector = lib.drmModeGetConnector(fd, connector_id)
    if not connector:
        LOG.warning("connector %d could not be read; leaving the mode alone", connector_id)
        return None
    try:
        info = connector.contents
        # Cheap guard against a struct layout that does not match the
        # installed libdrm: these would be nonsense if the fields were
        # misaligned, and acting on nonsense is worse than not acting.
        if info.connector_id != connector_id or not 0 < info.count_modes < 1024:
            LOG.warning(
                "libdrm returned an unexpected connector layout "
                "(id=%d modes=%d); leaving the mode alone",
                info.connector_id,
                info.count_modes,
            )
            return None
        wanted = None
        for index in range(info.count_modes):
            candidate = info.modes[index]
            if (candidate.hdisplay, candidate.vdisplay, candidate.vrefresh) == (
                width,
                height,
                refresh,
            ):
                # Copied, not referenced: the connector is freed below and the
                # mode has to outlive it.
                wanted = _DrmModeInfo.from_buffer_copy(candidate)
                break
        if wanted is None:
            offered = sorted(
                {
                    (
                        info.modes[i].hdisplay,
                        info.modes[i].vdisplay,
                        info.modes[i].vrefresh,
                    )
                    for i in range(info.count_modes)
                },
                reverse=True,
            )
            if all(rate == 0 for _w, _h, rate in offered):
                # Some drivers leave vrefresh at 0 and expect the rate to be
                # derived from clock/htotal/vtotal. Nothing here can match,
                # but that is this code's limitation rather than a bad
                # configuration, and kmssink would have picked a mode of the
                # right size regardless -- so warn and leave it to do that.
                LOG.warning(
                    "connector %d reports no refresh rates; leaving %dx%d to "
                    "the driver's own choice of mode",
                    connector_id,
                    width,
                    height,
                )
                return None
            raise DisplayError(
                f"connector {connector_id} does not offer {width}x{height}@{refresh}; "
                "it offers "
                + ", ".join(f"{w}x{h}@{r}" for w, h, r in offered[:8])
            )
    finally:
        lib.drmModeFreeConnector(connector)

    crtc = lib.drmModeGetCrtc(fd, crtc_id)
    if not crtc:
        LOG.warning("CRTC %d could not be read; leaving the mode alone", crtc_id)
        return None
    try:
        current = crtc.contents
        framebuffer = current.buffer_id
        already = (
            current.mode.hdisplay,
            current.mode.vdisplay,
            current.mode.vrefresh,
        )
    finally:
        lib.drmModeFreeCrtc(crtc)

    owned = None
    if background is not None:
        owned = _create_background_fb(lib, fd, width, height, background)
        if owned is not None:
            framebuffer = owned.fb_id

    if already == (width, height, refresh) and owned is None:
        return None
    if framebuffer == 0:
        # Nothing is lit and no colour was asked for, so there is no buffer
        # to scan out. The sink's own modeset is the path for that case.
        LOG.warning(
            "CRTC %d has no framebuffer; leaving the mode alone", crtc_id
        )
        return None

    connectors = (ctypes.c_uint32 * 1)(connector_id)
    result = lib.drmModeSetCrtc(
        fd, crtc_id, framebuffer, 0, 0, connectors, 1, ctypes.byref(wanted)
    )
    if result != 0:
        raise DisplayError(
            f"could not set {width}x{height}@{refresh} on connector "
            f"{connector_id}: {os.strerror(ctypes.get_errno())}"
        )
    LOG.info(
        "connector %d: mode set to %dx%d@%d (was %dx%d@%d)",
        connector_id,
        width,
        height,
        refresh,
        *already,
    )
    return owned
