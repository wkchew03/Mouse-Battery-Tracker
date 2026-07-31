"""Entry point.

    python -m mbt probe [--descriptors]   map HID collections (discovery)
    python -m mbt read                    one-shot battery for every mouse
    python -m mbt tray                    run the tray app (default)
"""

from __future__ import annotations

import argparse
import sys


def _auto_int(text: str) -> int:
    """Accept 0x372e, 372e or decimal for vid/pid arguments."""
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbt",
        description="Track gaming mouse battery levels from the Windows tray.",
    )
    sub = parser.add_subparsers(dest="command")

    probe = sub.add_parser(
        "probe",
        help="map every HID collection on the system (discovery tool)",
    )
    probe.add_argument("--vid", type=_auto_int, default=0, help="filter by vendor id")
    probe.add_argument("--pid", type=_auto_int, default=0, help="filter by product id")
    probe.add_argument(
        "--descriptors",
        action="store_true",
        help="read report descriptors from vendor collections (opens the device)",
    )
    probe.add_argument(
        "--read-features",
        action="store_true",
        help="read (never write) declared feature reports and hexdump them",
    )
    probe.add_argument(
        "--only-known",
        action="store_true",
        help="show only devices from known mouse vendors",
    )
    probe.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit raw enumeration as JSON (for capturing test fixtures)",
    )

    sub.add_parser("read", help="print battery for every detected mouse and exit")

    tray = sub.add_parser("tray", help="run the tray app")
    tray.add_argument(
        "--mock",
        action="store_true",
        help="drive the UI with fake devices (no hardware needed)",
    )
    tray.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between polls (default: 60)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "tray"

    if command == "probe":
        from . import probe

        return probe.run(
            vendor_id=args.vid,
            product_id=args.pid,
            descriptors=args.descriptors,
            read_features=args.read_features,
            only_known=args.only_known,
            as_json=args.as_json,
        )

    if command == "read":
        from . import app

        return app.print_readings()

    if command == "tray":
        from . import app

        return app.run_tray(
            mock=getattr(args, "mock", False),
            interval=getattr(args, "interval", 60.0),
        )

    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
