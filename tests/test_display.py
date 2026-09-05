import pytest

from viewwall.display import DisplayError, parse_kmsprint_all


KMSPRINT = """
Connector 0 (35) HDMI-A-1 (connected)
Crtc 0 (54)
Crtc 1 (73)
Crtc 2 (85)
Crtc 3 (97) 1920x1080@60.00 148.500
Plane 3 (86) fb-id: 670 (crtcs: 3) 0,0 (XR24 YU12 NV12)
Plane 4 (98) (crtcs: 1 2 3) 0,0 (XR24 YU12 NV12)
Plane 5 (109) (crtcs: 1 2 3) 0,0 (XR24 YU12 NV12)
Plane 6 (120) (crtcs: 1 2) 0,0 (XR24 YU12 NV12)
"""


def test_parse_kmsprint_selects_unused_compatible_yuv_planes() -> None:
    (state,) = parse_kmsprint_all(KMSPRINT)
    assert state.connector_id == 35
    assert state.crtc_id == 97
    assert (state.width, state.height) == (1920, 1080)
    assert state.plane_ids == (98, 109)


def test_a_plane_shortage_names_the_likely_cause(monkeypatch):
    from viewwall import display as display_module
    from viewwall.config import DisplayConfig

    # A primary plane only, as the legacy and fkms drivers expose.
    text = (
        "Connector 0 (35) HDMI-A-1 (connected)\n"
        "Crtc 0 (97) 1920x1080@60.00\n"
        "Plane 0 (86) fb-id: 1 (crtcs: 0) 0,0 1920x1080 (XR24)\n"
    )
    monkeypatch.setattr(display_module, "_run_kmsprint", lambda: text)
    with pytest.raises(DisplayError, match="full KMS"):
        display_module.detect_displays([DisplayConfig(name="main")], {"main": 1})


def _sysfs(
    tmp_path,
    status: str = "connected",
    modes: str = "1920x1080\n1280x720\n",
    connector_id: str = "35",
):
    connector = tmp_path / "card0-HDMI-A-1"
    connector.mkdir()
    (connector / "status").write_text(status)
    (connector / "modes").write_text(modes)
    (connector / "connector_id").write_text(connector_id)
    return tmp_path


def test_current_modes_reads_the_active_resolution(tmp_path) -> None:
    from viewwall.display import current_modes

    assert current_modes(_sysfs(tmp_path)) == {35: (1920, 1080)}


def test_current_modes_skips_disconnected_outputs(tmp_path) -> None:
    from viewwall.display import current_modes

    assert current_modes(_sysfs(tmp_path, status="disconnected")) == {}


def test_current_modes_is_empty_when_sysfs_is_absent(tmp_path) -> None:
    # Falls back to a full probe rather than assuming the mode is unchanged.
    from viewwall.display import current_modes

    assert current_modes(tmp_path / "nonexistent") == {}


def test_current_modes_ignores_an_unparsable_mode(tmp_path) -> None:
    from viewwall.display import current_modes

    assert current_modes(_sysfs(tmp_path, modes="\n")) == {}


def test_current_modes_skips_a_connector_with_no_id(tmp_path) -> None:
    # The kernel does not expose connector_id everywhere; such a connector
    # cannot be matched to a display, so the caller falls back to a probe.
    from viewwall.display import current_modes

    sysfs = _sysfs(tmp_path)
    (sysfs / "card0-HDMI-A-1" / "connector_id").unlink()
    assert current_modes(sysfs) == {}


def test_current_modes_reports_each_connector(tmp_path) -> None:
    from viewwall.display import current_modes

    for name, connector_id, mode in (
        ("card0-HDMI-A-1", "35", "1920x1080\n"),
        ("card0-HDMI-A-2", "36", "1280x720\n"),
    ):
        connector = tmp_path / name
        connector.mkdir()
        (connector / "status").write_text("connected")
        (connector / "modes").write_text(mode)
        (connector / "connector_id").write_text(connector_id)
    assert current_modes(tmp_path) == {35: (1920, 1080), 36: (1280, 720)}


_TWO_DISPLAYS = """Connector 0 (32) HDMI-A-1 (connected)
  Crtc 0 (35) 1920x1080@60.00
    Plane 0 (40) fb-id: 12 (crtcs: 0 1) 0,0 (YU12)
    Plane 1 (41) (crtcs: 0) 0,0 (YU12)
    Plane 2 (42) (crtcs: 1) 0,0 (YU12)
Connector 1 (52) HDMI-A-2 (connected)
  Crtc 1 (55) 1280x720@60.00
    Plane 3 (43) (crtcs: 1) 0,0 (YU12)
"""


def test_a_second_display_does_not_split_the_selection() -> None:
    # Connector and CRTC were tracked independently, so the connector came
    # from the first display while the CRTC, mode and planes came from the
    # second: kmssink was then given a connector paired with another CRTC's
    # planes, and the layout was resolved at the wrong resolution.
    state, _second = parse_kmsprint_all(_TWO_DISPLAYS)
    assert state.connector_id == 32
    assert state.crtc_id == 35
    assert state.crtc_index == 0
    assert (state.width, state.height) == (1920, 1080)


def test_planes_come_from_the_chosen_crtc_only() -> None:
    state, _second = parse_kmsprint_all(_TWO_DISPLAYS)
    # 40 is already scanning out (fb-id), 42 and 43 belong to the other CRTC.
    assert state.plane_ids == (41,)


def test_a_disconnected_output_is_skipped() -> None:
    text = """Connector 0 (32) HDMI-A-1 (disconnected)
Connector 1 (52) HDMI-A-2 (connected)
  Crtc 1 (55) 1280x720@60.00
    Plane 3 (43) (crtcs: 1) 0,0 (YU12)
"""
    (state,) = parse_kmsprint_all(text)
    assert state.connector_id == 52
    assert (state.width, state.height) == (1280, 720)


def test_a_configured_connector_selects_its_whole_block() -> None:
    # Substituting only the id used to pair the chosen connector with the
    # first one's CRTC, mode and planes -- the exact mismatch the override
    # exists to avoid.
    _first, state = parse_kmsprint_all(_TWO_DISPLAYS)
    assert state.connector_id == 52
    assert state.crtc_id == 55
    assert (state.width, state.height) == (1280, 720)
    # Both planes declare "crtcs: 1", so both can drive the chosen CRTC.
    assert state.plane_ids == (42, 43)


def test_an_unknown_connector_is_named_in_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viewwall import display as display_module
    from viewwall.config import DisplayConfig

    monkeypatch.setattr(display_module, "_run_kmsprint", lambda: _TWO_DISPLAYS)
    with pytest.raises(DisplayError, match="99"):
        display_module.detect_displays(
            [DisplayConfig(name="main", connector_id=99)], {"main": 1}
        )


TWO_DISPLAYS = """
Connector 0 (35) HDMI-A-1 (connected)
Crtc 0 (54) 1920x1080@60.00 148.500
Connector 1 (36) HDMI-A-2 (connected)
Crtc 1 (64) 1280x720@60.00 74.250
Plane 0 (200) (crtcs: 0) 0,0 (XR24 YU12 NV12)
Plane 1 (201) (crtcs: 0) 0,0 (XR24 YU12 NV12)
Plane 2 (210) (crtcs: 1) 0,0 (XR24 YU12 NV12)
Plane 3 (220) (crtcs: 0 1) 0,0 (XR24 YU12 NV12)
Plane 4 (221) (crtcs: 0 1) 0,0 (XR24 YU12 NV12)
"""

# left may use 300 (shared) or 301 (its own); right may use only 300.
CONTENDED = """
Connector 0 (35) HDMI-A-1 (connected)
Crtc 0 (54) 1920x1080@60.00 148.500
Connector 1 (36) HDMI-A-2 (connected)
Crtc 1 (64) 1280x720@60.00 74.250
Plane 0 (300) (crtcs: 0 1) 0,0 (XR24 YU12 NV12)
Plane 1 (301) (crtcs: 0) 0,0 (XR24 YU12 NV12)
"""


def _detect(monkeypatch, text, configs, demand):
    from viewwall import display as display_module

    monkeypatch.setattr(display_module, "_run_kmsprint", lambda: text)
    return display_module.detect_displays(configs, demand)


def test_parse_kmsprint_all_reads_every_connected_output() -> None:
    from viewwall.display import parse_kmsprint_all

    states = parse_kmsprint_all(TWO_DISPLAYS)
    assert [state.connector_id for state in states] == [35, 36]
    assert [(state.width, state.height) for state in states] == [
        (1920, 1080),
        (1280, 720),
    ]
    # A plane that can drive either CRTC is offered to both; choosing is the
    # allocator's job, not the parser's.
    assert 220 in states[0].plane_ids and 220 in states[1].plane_ids


def test_no_plane_is_given_to_two_displays(monkeypatch: pytest.MonkeyPatch) -> None:
    from viewwall.config import DisplayConfig

    configs = (
        DisplayConfig(name="left", connector_id=35),
        DisplayConfig(name="right", connector_id=36),
    )
    resolved = _detect(monkeypatch, TWO_DISPLAYS, configs, {"left": 3, "right": 2})
    assigned = [
        plane for state in resolved.values() for plane in state.plane_ids
    ]
    assert len(assigned) == len(set(assigned))
    assert len(resolved["left"].plane_ids) == 3
    assert len(resolved["right"].plane_ids) == 2
    # Each display keeps its own mode.
    assert (resolved["left"].width, resolved["left"].height) == (1920, 1080)
    assert (resolved["right"].width, resolved["right"].height) == (1280, 720)


def test_a_shared_plane_is_not_wasted_on_a_display_with_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Handing planes out greedily gives 300 to left, whose only alternative
    # was 301, and leaves right with nothing -- failing a request the hardware
    # can satisfy.
    from viewwall.config import DisplayConfig

    configs = (
        DisplayConfig(name="left", connector_id=35),
        DisplayConfig(name="right", connector_id=36),
    )
    resolved = _detect(monkeypatch, CONTENDED, configs, {"left": 1, "right": 1})
    assert resolved["left"].plane_ids == (301,)
    assert resolved["right"].plane_ids == (300,)


def test_a_genuinely_impossible_demand_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viewwall.config import DisplayConfig

    configs = (
        DisplayConfig(name="left", connector_id=35),
        DisplayConfig(name="right", connector_id=36),
    )
    with pytest.raises(DisplayError, match="cannot all be satisfied"):
        _detect(monkeypatch, CONTENDED, configs, {"left": 2, "right": 1})


def test_an_unconnected_connector_id_names_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viewwall.config import DisplayConfig

    configs = (DisplayConfig(name="porch_tv", connector_id=99),)
    with pytest.raises(DisplayError, match="porch_tv.*99 is not a connected"):
        _detect(monkeypatch, TWO_DISPLAYS, configs, {"porch_tv": 1})


def test_a_pinned_lone_display_does_not_fall_back_to_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "First connected" applies only when nothing is configured. A display
    # that names a connector gets that one, with its own mode and planes.
    from viewwall.config import DisplayConfig

    configs = (DisplayConfig(name="office", connector_id=36),)
    resolved = _detect(monkeypatch, TWO_DISPLAYS, configs, {"office": 1})
    state = resolved["office"]
    assert state.connector_id == 36
    assert (state.width, state.height) == (1280, 720)
    assert state.plane_ids == (210,)


def test_an_unconfigured_display_takes_the_first_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viewwall.config import DisplayConfig

    configs = (DisplayConfig(name="main"),)
    resolved = _detect(monkeypatch, TWO_DISPLAYS, configs, {"main": 1})
    assert resolved["main"].connector_id == 35
    assert (resolved["main"].width, resolved["main"].height) == (1920, 1080)
