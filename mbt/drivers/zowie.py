"""BenQ ZOWIE presence, with no battery protocol.

There is no battery query here because none is known. The U2-DW exposes two
vendor collections -- report 0x08 out on usage page 0xff03, report 0x09 in on
0xff04, 15 bytes each -- but it never answers and never volunteers anything,
across ~5 minutes of passive capture. There is no vendor configurator and no
public protocol to copy from, so this driver writes nothing to the device at
all; it only reads what USB enumeration already reports.

What it does provide is presence: the mouse appears in the tray and the HUD
alongside the others, showing "no battery data" where a percentage would be,
rather than being silently absent.

**`online` here means enumerated, not awake.** Every other driver proves a
mouse is on by completing a protocol exchange. This one cannot, so a receiver
left plugged in with the mouse switched off still reads as connected, and
"last active" tracks the dongle rather than the mouse. Nothing is inferred
beyond "the operating system can see this device".

Selection deliberately matches more than vid/pid: 0x04A5 is BenQ, whose
monitors also expose HID collections. A device is claimed only when the same
(vid, pid, serial) exposes both a Generic Desktop mouse collection and the
0xff03 vendor collection -- which no display does.

STATUS: enumeration captured from a ZOWIE U2-DW (04a5:800a) -- interface 0 for
the mouse collection, interface 1 for both vendor collections. Battery is
unavailable by protocol, not unverified.
"""

from __future__ import annotations

from .base import DeviceInfo, Reading, matches, register

VENDOR_ZOWIE = 0x04A5

# Vendor-defined collections. The command channel is the one a future protocol
# would write to, so it is the entry this driver claims.
USAGE_PAGE_COMMAND = 0xFF03  # report 0x08, 15-byte output
USAGE_PAGE_EVENT = 0xFF04  # report 0x09, 15-byte input

USAGE_PAGE_GENERIC_DESKTOP = 0x0001
USAGE_MOUSE = 0x02

# Shown where a percentage would go. Worded to stay true in both places it
# surfaces: live in the hero card, and replayed from the stored record in the
# "Recently used" grid, where "connected" would be a lie.
NO_BATTERY = "no battery data"

# The USB strings say only "BenQ ZOWIE Gaming Mouse", which every model reports,
# so they never name the actual mouse. Anything not listed keeps the USB strings.
MODEL_NAMES = {0x800A: "ZOWIE U2-DW"}

# Constant because the read is pure enumeration -- there is nothing to query.
PRESENT = Reading(online=True, bucket=NO_BATTERY)


def group_key(info: DeviceInfo) -> tuple:
    """Which physical device an enumeration entry belongs to.

    Collections of one device share vid/pid/serial and differ by interface and
    usage, so this is what ties the mouse collection to the vendor one.
    """
    return (
        info.get("vendor_id"),
        info.get("product_id"),
        (info.get("serial_number") or "").strip(),
    )


def is_mouse_collection(info: DeviceInfo) -> bool:
    return matches(
        info,
        vendor_id=VENDOR_ZOWIE,
        usage_page=USAGE_PAGE_GENERIC_DESKTOP,
        usage=USAGE_MOUSE,
    )


def is_command_collection(info: DeviceInfo) -> bool:
    return matches(info, vendor_id=VENDOR_ZOWIE, usage_page=USAGE_PAGE_COMMAND)


class ZowieDriver:
    name = "zowie"
    vendor_ids = frozenset({VENDOR_ZOWIE})

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """The 0xff03 collection of every device that is also a mouse."""
        command: dict[tuple, DeviceInfo] = {}
        mice: set[tuple] = set()
        for info in infos:
            key = group_key(info)
            if is_mouse_collection(info):
                mice.add(key)
            elif is_command_collection(info):
                command.setdefault(key, info)
        return [info for key, info in command.items() if key in mice]

    def label(self, info: DeviceInfo) -> str | None:
        return MODEL_NAMES.get(info.get("product_id"))

    def read(self, info: DeviceInfo) -> Reading:
        """Presence only. Never opens the device -- there is nothing to ask it."""
        return PRESENT


register(ZowieDriver())
