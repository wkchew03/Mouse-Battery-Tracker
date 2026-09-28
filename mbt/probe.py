"""Discovery CLI: map every HID collection on the system.

This is the ground-truth tool that all driver work is built on. Run it once
per mouse (powered on and connected) and record the output -- the vid/pid/
interface/usage tuple it prints is exactly what each driver's `candidates()`
has to match against.

With --descriptors it also reads each vendor collection's report descriptor,
which reveals the supported report IDs and their exact payload sizes. That
turns "guess the protocol" into "read the sizes off the device".
"""

from __future__ import annotations

import io
import json
from collections import defaultdict
from contextlib import redirect_stdout
from typing import NamedTuple

from . import hidio, hidparse
from .drivers.base import KNOWN_VENDORS, describe_device, device_key
from .drivers.adopted import is_mouse_collection


def is_vendor_defined(info: dict) -> bool:
    """Vendor-defined usage pages (0xff00-0xffff) carry the battery protocols."""
    usage_page = info.get("usage_page") or 0
    return 0xFF00 <= usage_page <= 0xFFFF


def is_standard_battery(info: dict) -> bool:
    """Standard HID battery usages -- readable with no vendor protocol at all."""
    usage_page = info.get("usage_page") or 0
    usage = info.get("usage") or 0
    return usage_page == 0x85 or (usage_page == 0x06 and usage == 0x20)


def _group_key(info: dict) -> tuple:
    return (info["vendor_id"], info["product_id"], info.get("serial_number") or "")


def _format_collection(info: dict) -> str:
    interface = info.get("interface_number")
    interface_text = f"if={interface}" if interface is not None else "if=-"
    usage_page = info.get("usage_page") or 0
    usage = info.get("usage") or 0

    label = hidparse.usage_page_name(usage_page)
    detail = hidparse.usage_name(usage_page, usage)
    if detail:
        label = f"{label} / {detail}"

    line = f"    {interface_text:<6} up=0x{usage_page:04x}/0x{usage:04x}  {label:<32}"
    if is_standard_battery(info):
        line += "  <== STANDARD BATTERY USAGE"
    elif is_vendor_defined(info):
        line += "  <== candidate"
    return line.rstrip()


def _print_descriptor(info: dict, indent: str = "        ") -> None:
    try:
        raw = hidio.report_descriptor(info["path"])
    except Exception as exc:
        print(f"{indent}(descriptor unavailable: {exc})")
        return

    descriptor = hidparse.parse(raw)
    print(f"{indent}descriptor: {len(raw)} bytes, {len(descriptor.reports)} report(s)")
    for report_id, sizes in sorted(descriptor.reports.items()):
        rid = "none" if report_id == 0 else f"0x{report_id:02x}"
        print(f"{indent}  report id {rid:<6} {sizes.summary()}")
    if descriptor.has_battery_usage():
        print(f"{indent}  ** references the Battery System usage page **")


def hexdump(data: bytes, indent: str = "          ") -> None:
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{indent}{offset:02x}: {hexpart:<47}  {text}")


def _read_features(info: dict, indent: str = "        ") -> None:
    """Read (never write) every feature report the collection declares.

    get_feature_report is a pure read IOCTL -- it returns the device's current
    feature state without sending it a command. That makes this completely safe
    to run against an unknown device, unlike writing speculative command bytes.
    """
    try:
        raw = hidio.report_descriptor(info["path"])
    except Exception as exc:
        print(f"{indent}(descriptor unavailable: {exc})")
        return

    descriptor = hidparse.parse(raw)
    feature_reports = [
        (rid, sizes.feature) for rid, sizes in descriptor.reports.items() if sizes.feature
    ]
    if not feature_reports:
        print(f"{indent}no feature reports declared")
        return

    try:
        with hidio.open_path(info["path"]) as dev:
            for report_id, size in feature_reports:
                # +1 for the report ID byte that hidapi prepends to the buffer.
                try:
                    data = bytes(dev.get_feature_report(report_id, size + 1))
                except Exception as exc:
                    print(f"{indent}report 0x{report_id:02x}: read failed ({exc})")
                    continue
                if not data:
                    print(f"{indent}report 0x{report_id:02x}: empty response")
                    continue
                nonzero = sum(1 for b in data if b)
                print(
                    f"{indent}report 0x{report_id:02x}: {len(data)} bytes, "
                    f"{nonzero} non-zero"
                )
                hexdump(data, indent + "  ")
    except Exception as exc:
        print(f"{indent}(could not open: {exc})")


def run(
    vendor_id: int = 0,
    product_id: int = 0,
    descriptors: bool = False,
    read_features: bool = False,
    only_known: bool = False,
    as_json: bool = False,
) -> int:
    infos = hidio.enumerate_devices(vendor_id, product_id)

    if as_json:
        payload = []
        for info in infos:
            entry = {k: v for k, v in info.items() if k != "path"}
            entry["path"] = info["path"].decode("utf-8", "replace")
            entry["key"] = device_key(info)
            payload.append(entry)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for info in infos:
        grouped[_group_key(info)].append(info)

    if only_known:
        grouped = {k: v for k, v in grouped.items() if k[0] in KNOWN_VENDORS}

    shown = sum(len(v) for v in grouped.values())
    print(f"{shown} HID collection(s) across {len(grouped)} device(s).\n")

    for (vid, pid, serial), collections in grouped.items():
        vendor = KNOWN_VENDORS.get(vid)
        header = f"=== {vid:04x}:{pid:04x}"
        if vendor:
            header += f"  [{vendor}]"
        header += " ==="
        print(header)

        first = collections[0]
        print(f"  {describe_device(first)}")
        print(f"  serial={serial or '(none)'}   key={device_key(first)}")

        for info in collections:
            print(_format_collection(info))
            interesting = is_vendor_defined(info) or is_standard_battery(info)
            if descriptors and interesting:
                _print_descriptor(info)
            if read_features and interesting:
                _read_features(info)
        print()

    if not any(k[0] in KNOWN_VENDORS for k in grouped):
        print("No devices from a known mouse vendor were found.")
        print("Power on each mouse (or plug in its dongle) and re-run.")

    return 0


# ---- the HUD's "Add mouse" scan ----------------------------------------------


class UnknownMouse(NamedTuple):
    key: str
    vendor_id: int
    product_id: int
    name: str
    note: str
    # This probe's full output for the device: what adding a driver starts from.
    report: str


def unknown_mice(infos: list[dict], claimed: set[str]) -> list[list[dict]]:
    """The collections of every mouse-like device that no driver claimed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for info in infos:
        key = device_key(info)
        if key not in claimed:
            groups[key].append(info)
    return [c for c in groups.values() if any(is_mouse_collection(i) for i in c)]


def diagnose(collections: list[dict]) -> str:
    """One line on why no driver reads this device, from enumeration alone."""
    vendor = KNOWN_VENDORS.get(collections[0]["vendor_id"])
    if vendor:
        return f"{vendor} is supported, but not this model yet."
    if any(is_standard_battery(i) for i in collections):
        return "Declares a standard HID battery usage, which no driver reads yet."
    if any(is_vendor_defined(i) for i in collections):
        return "Has a vendor channel, so its battery needs a new driver."
    return "Exposes no channel a battery could be read from."


def report(vendor_id: int, product_id: int) -> str:
    """`probe --descriptors --read-features` for one device, as text."""
    out = io.StringIO()
    with redirect_stdout(out):
        run(vendor_id, product_id, descriptors=True, read_features=True)
    return out.getvalue()


def scan() -> tuple[dict[str, str], list[UnknownMouse]]:
    """Every tracked mouse (key -> label), and every mouse nothing claims.

    Opens devices, so it must run on the poll thread, never the UI's.
    """
    from .app import discover
    from .drivers.base import resolve_identity, resolve_label

    infos = hidio.enumerate_devices()
    found = discover()
    tracked = {resolve_identity(d, i): resolve_label(d, i) for d, i in found}
    claimed = {device_key(i) for _, i in found}
    unknown = []
    for collections in unknown_mice(infos, claimed):
        first = collections[0]
        vid, pid = first["vendor_id"], first["product_id"]
        unknown.append(UnknownMouse(
            device_key(first), vid, pid, describe_device(first),
            diagnose(collections), report(vid, pid),
        ))
    return tracked, unknown
