from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from .config import AppConfig, ConfigError, load_config
from .display import DisplayError, detect_displays
from .gst_runtime import RuntimeDependencyError, WallRuntime
from .journal import install as install_logging
from .layout import resolve_layout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DRM/KMS RTSP camera wall")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("/etc/viewwall/viewwall.toml"),
        help="configuration file (default: /etc/viewwall/viewwall.toml)",
    )
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="run the camera wall")
    subparsers.add_parser("validate", help="validate configuration without opening DRM")
    layout = subparsers.add_parser("layout", help="print resolved viewport rectangles")
    layout.add_argument("--width", type=int)
    layout.add_argument("--height", type=int)
    return parser


def _print_layout(config: AppConfig, width: int | None, height: int | None) -> None:
    # One probe for the whole card, not one per display: kmsprint opens the DRM
    # device, so a probe per display would multiply that cost for no gain. It
    # runs only if some display needs a mode nothing else supplies. Demand is
    # zero because this command reports rectangles and never binds a plane.
    needs_probe = width is None and any(
        display.width is None for display in config.displays
    )
    states = (
        detect_displays(config.displays, {display.name: 0 for display in config.displays})
        if needs_probe
        else {}
    )
    for display in config.displays:
        viewports = config.viewports_for(display)
        if width is not None and height is not None:
            output_width, output_height = width, height
        elif display.width is not None:
            output_width, output_height = display.width, display.height
        else:
            state = states[display.name]
            output_width, output_height = state.width, state.height
        resolved = resolve_layout(
            viewports, output_width, output_height, config.layout_for(display)
        )
        print(f"display={display.name} output={output_width}x{output_height}")
        for viewport in viewports:
            item = resolved[viewport.name]
            print(
                f"{viewport.name}: full={item.full.x},{item.full.y} {item.full.width}x{item.full.height} "
                f"render={item.render.x},{item.render.y} {item.render.width}x{item.render.height} "
                f"feeds={','.join(viewport.feeds)}"
            )


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    install_logging(
        getattr(logging, args.log_level),
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    command = args.command or "run"
    try:
        config = load_config(args.config)
        if command == "validate":
            displays = len(config.displays)
            suffix = "" if displays == 1 else f", {displays} displays"
            print(
                f"valid: {len(config.feeds)} feeds, {len(config.viewports)} viewports"
                f"{suffix}"
            )
            return
        if command == "layout":
            if (args.width is None) != (args.height is None):
                raise ConfigError("--width and --height must be supplied together")
            _print_layout(config, args.width, args.height)
            return
        WallRuntime(config).run()
    except (ConfigError, DisplayError, RuntimeDependencyError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main(sys.argv[1:])

