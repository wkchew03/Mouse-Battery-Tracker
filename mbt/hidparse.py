"""Minimal HID report descriptor parser.

We only need two things out of a descriptor: which report IDs exist, and how
big each one's input/output/feature payload is. That is enough to know exactly
what to send to an undocumented vendor collection instead of guessing.

Reference: HID 1.11 section 6.2.2 (item format).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# bSize field (low 2 bits of the item prefix) encodes 0, 1, 2 or 4 data bytes.
_ITEM_DATA_SIZES = {0: 0, 1: 1, 2: 2, 3: 4}

# Item tags we care about (prefix with the bSize bits masked off).
_TAG_USAGE_PAGE = 0x04
_TAG_REPORT_ID = 0x84
_TAG_REPORT_SIZE = 0x74
_TAG_REPORT_COUNT = 0x94
_TAG_INPUT = 0x80
_TAG_OUTPUT = 0x90
_TAG_FEATURE = 0xB0

_MAIN_TAGS = {_TAG_INPUT: "input", _TAG_OUTPUT: "output", _TAG_FEATURE: "feature"}

USAGE_PAGE_NAMES = {
    0x01: "Generic Desktop",
    0x02: "Simulation",
    0x06: "Generic Device Controls",
    0x07: "Keyboard",
    0x08: "LED",
    0x09: "Button",
    0x0B: "Telephony",
    0x0C: "Consumer",
    0x0D: "Digitizer",
    0x85: "Battery System",
}

_GENERIC_DESKTOP_USAGES = {
    0x01: "Pointer",
    0x02: "Mouse",
    0x04: "Joystick",
    0x05: "Gamepad",
    0x06: "Keyboard",
    0x80: "System Control",
}


def usage_page_name(usage_page: int | None) -> str:
    if usage_page is None:
        return "?"
    if usage_page in USAGE_PAGE_NAMES:
        return USAGE_PAGE_NAMES[usage_page]
    if 0xFF00 <= usage_page <= 0xFFFF:
        return "vendor-defined"
    return "?"


def usage_name(usage_page: int | None, usage: int | None) -> str:
    if usage_page == 0x01:
        return _GENERIC_DESKTOP_USAGES.get(usage, "")
    # Generic Device Controls usage 0x20 is Battery Strength -- a device
    # exposing this gives us battery for free, with no vendor protocol at all.
    if usage_page == 0x06 and usage == 0x20:
        return "Battery Strength"
    return ""


@dataclass
class ReportSizes:
    """Payload sizes in bytes for one report ID, excluding the report ID byte."""

    input: int = 0
    output: int = 0
    feature: int = 0

    def summary(self) -> str:
        parts = []
        for kind in ("input", "output", "feature"):
            size = getattr(self, kind)
            if size:
                parts.append(f"{kind}={size}B")
        return " ".join(parts) or "(empty)"


@dataclass
class Descriptor:
    reports: dict[int, ReportSizes] = field(default_factory=dict)
    usage_pages: list[int] = field(default_factory=list)

    def has_battery_usage(self) -> bool:
        """True if the descriptor references a standard battery usage page."""
        return 0x85 in self.usage_pages


def parse(data: bytes) -> Descriptor:
    """Parse a raw report descriptor into report IDs and payload sizes."""
    result = Descriptor()
    bits: dict[int, dict[str, int]] = {}

    report_id = 0
    report_size = 0
    report_count = 0

    i = 0
    length = len(data)
    while i < length:
        prefix = data[i]
        i += 1

        if prefix == 0xFE:  # long item: [bDataSize, bLongItemTag, data...]
            if i >= length:
                break
            data_size = data[i]
            i += 2 + data_size
            continue

        size = _ITEM_DATA_SIZES[prefix & 0x03]
        tag = prefix & 0xFC
        value = int.from_bytes(data[i : i + size], "little") if size else 0
        i += size

        if tag == _TAG_REPORT_ID:
            report_id = value
        elif tag == _TAG_REPORT_SIZE:
            report_size = value
        elif tag == _TAG_REPORT_COUNT:
            report_count = value
        elif tag == _TAG_USAGE_PAGE:
            if value not in result.usage_pages:
                result.usage_pages.append(value)
        elif tag in _MAIN_TAGS:
            entry = bits.setdefault(report_id, {"input": 0, "output": 0, "feature": 0})
            entry[_MAIN_TAGS[tag]] += report_size * report_count

    for rid, kinds in sorted(bits.items()):
        result.reports[rid] = ReportSizes(
            input=-(-kinds["input"] // 8),
            output=-(-kinds["output"] // 8),
            feature=-(-kinds["feature"] // 8),
        )
    return result
