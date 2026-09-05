from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from .config import DisplayConfig

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
            # Only a lone unconfigured display omits a connector, so this is
            # the no-[displays] case. Other screens may well be attached: the
            # first connected one is used and the rest are left alone, which
            # is what driving them requires a table to say.
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
