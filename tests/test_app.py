from pathlib import Path

import pytest

from viewwall import app as app_module
from viewwall.config import ConfigError
from viewwall.display import DisplayState


_SINGLE_DISPLAY = """
[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = "1/3"
height = 1
feeds = ["camera"]

[[viewports]]
x = "1/3"
y = 0
width = "2/3"
height = 1
feeds = ["camera"]
"""


def _write(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "viewwall.toml"
    config_path.write_text(body, encoding="utf-8")
    return config_path


def test_validate_reports_the_counts(tmp_path: Path, capsys) -> None:
    app_module.main(["-c", str(_write(tmp_path, _SINGLE_DISPLAY)), "validate"])
    assert capsys.readouterr().out == "valid: 1 feeds, 2 viewports\n"


def test_validate_names_the_display_count_only_when_there_is_a_choice(
    tmp_path: Path, capsys
) -> None:
    body = """
[displays.left]
connector_id = 32

[displays.right]
connector_id = 33

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "left"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
display = "right"
"""
    app_module.main(["-c", str(_write(tmp_path, body)), "validate"])
    assert capsys.readouterr().out == "valid: 1 feeds, 2 viewports, 2 displays\n"


def test_an_invalid_configuration_exits_non_zero(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        app_module.main(["-c", str(_write(tmp_path, "[feeds]\n")), "validate"])
    assert excinfo.value.code == 1


def test_a_missing_configuration_file_exits_non_zero(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        app_module.main(["-c", str(tmp_path / "absent.toml"), "validate"])
    assert excinfo.value.code == 1


def test_layout_uses_the_given_size_without_probing(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A size on the command line is the whole answer, so the DRM device is
    # never opened. Anything else would make "layout" unusable off the Pi.
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("detect_displays must not run when a size is given")

    monkeypatch.setattr(app_module, "detect_displays", fail)
    app_module.main(
        [
            "-c",
            str(_write(tmp_path, _SINGLE_DISPLAY)),
            "layout",
            "--width",
            "1920",
            "--height",
            "1080",
        ]
    )
    out = capsys.readouterr().out
    assert "display=main output=1920x1080" in out
    # A third of 1920 lands exactly, which is what the fractions are for.
    assert "viewport1: full=0,0 640x1080" in out
    assert "viewport2: full=640,0 1280x1080" in out


def test_layout_probes_when_no_size_is_configured(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, int]] = []

    def fake_detect(configs, demand):
        seen.append(dict(demand))
        return {
            config.name: DisplayState(
                connector_id=32,
                crtc_index=1,
                crtc_id=64,
                width=1280,
                height=720,
                plane_ids=(),
            )
            for config in configs
        }

    monkeypatch.setattr(app_module, "detect_displays", fake_detect)
    app_module.main(["-c", str(_write(tmp_path, _SINGLE_DISPLAY)), "layout"])
    assert "display=main output=1280x720" in capsys.readouterr().out
    # This command reports rectangles and never binds a plane, so it must not
    # ask for any: a demand of one per viewport would fail on a card whose
    # planes are already spoken for.
    assert seen == [{"main": 0}]


def test_layout_prefers_a_pinned_size_to_a_probe(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a pinned display needs no probe")

    monkeypatch.setattr(app_module, "detect_displays", fail)
    body = """
[displays.main]
connector_id = 32
width = 800
height = 600

[feeds.camera]
uri = "rtsp://nvr.invalid/feed"

[[viewports]]
x = 0
y = 0
width = 1
height = 1
feeds = ["camera"]
"""
    app_module.main(["-c", str(_write(tmp_path, body)), "layout"])
    assert "display=main output=800x600" in capsys.readouterr().out


def test_layout_requires_width_and_height_together(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        app_module.main(
            ["-c", str(_write(tmp_path, _SINGLE_DISPLAY)), "layout", "--width", "1920"]
        )
    assert excinfo.value.code == 1


def test_the_default_command_is_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No subcommand means run the wall, which is what the service does.
    started: list[object] = []

    class FakeRuntime:
        def __init__(self, config: object) -> None:
            started.append(config)

        def run(self) -> None:
            started.append("ran")

    monkeypatch.setattr(app_module, "WallRuntime", FakeRuntime)
    app_module.main(["-c", str(_write(tmp_path, _SINGLE_DISPLAY))])
    assert started[-1] == "ran"
