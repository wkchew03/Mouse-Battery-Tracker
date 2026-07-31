"""Core types shared by every battery driver.

A driver knows how to talk to one vendor's mice. It does two things: pick which
HID collection to talk to out of the several each device exposes, and turn a
battery query into a `Reading`.

Picking the collection is the part that is easy to get wrong. Every mouse here
exposes 3-6 collections -- the mouse collection, a keyboard collection, a
consumer-control collection, and one or more vendor-defined ones -- and only
the vendor-defined collection answers protocol commands. Matching on vid/pid
alone will happily open the wrong one and then time out forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

# One entry as returned by hid.enumerate(). The keys used across this package
# are: vendor_id, product_id, interface_number, usage_page, usage, path,
# serial_number, manufacturer_string, product_string.
DeviceInfo = dict

# Vendors we have (or plan to have) a driver for. Used by the probe output to
# highlight interesting devices and by drivers to declare what they handle.
VENDOR_RAZER = 0x1532
VENDOR_LOGITECH = 0x046D
VENDOR_PULSAR = 0x3554
VENDOR_PIAO = 0x372E
VENDOR_COMPX = 0x25A7

KNOWN_VENDORS = {
    VENDOR_RAZER: "Razer",
    VENDOR_LOGITECH: "Logitech",
    VENDOR_PULSAR: "Pulsar",
    0x3710: "Pulsar (8K dongle)",
    VENDOR_PIAO: "PIAO / CompX family",
    VENDOR_COMPX: "CompX",
    0x3151: "Lamzu / OEM",
}


@dataclass(frozen=True)
class Reading:
    """The result of one battery query.

    `online` distinguishes "the mouse is powered off" from "the read failed".
    A 2.4GHz dongle stays enumerated even when its mouse is off, so presence of
    the device tells us nothing on its own -- only a successful protocol
    exchange does.
    """

    online: bool
    percent: int | None = None
    charging: bool | None = None
    millivolts: int | None = None
    # How the mouse is attached right now ("wired" / "2.4 GHz" / ...). One
    # physical mouse can appear under different USB ids per connection, so the
    # entry is merged and this says which mode produced the reading.
    connection: str | None = None
    # Identity reported by the device itself, when it can name the specific
    # mouse rather than the receiver. A Pulsar dongle can be re-paired to a
    # different mouse, so keying on the dongle would merge two mice into one
    # entry; this overrides the USB-derived key when present.
    device_id: str | None = None
    # Set when the device only reports coarse buckets rather than a true
    # percentage (older Logitech mice via HID++ feature 0x1000). Surfaced as-is
    # rather than converted into a fake number.
    bucket: str | None = None

    def describe(self) -> str:
        """Human-readable battery state for the tray menu."""
        if not self.online:
            return "off"
        if self.percent is not None:
            text = f"{self.percent}%"
        elif self.bucket is not None:
            text = self.bucket
        elif self.charging:
            # Some firmware reports "on external power" without a level.
            return "charging"
        else:
            text = "unknown"
        if self.charging:
            text += " (charging)"
        return text


OFFLINE = Reading(online=False)


def is_placeholder_serial(serial: str) -> bool:
    """True for firmware placeholder serials that don't identify anything.

    Several of these mice ship a constant serial burned into every unit -- the
    PIAO dongle reports "000000000001" -- so using it as an identity key would
    merge distinct devices. Conservative rule: zero-padded values that reduce
    to nothing or a single digit are placeholders. A real serial like
    "0000123456" or "A00WA4221PPVD6" survives.
    """
    stripped = serial.strip().lstrip("0")
    return not stripped or (stripped.isdigit() and len(stripped) == 1)


def device_key(info: DeviceInfo) -> str:
    """Stable identity for a physical device, used as the persistence key.

    Prefers the serial number, falling back to vid:pid when the device only
    reports a placeholder.
    """
    vid = info["vendor_id"]
    pid = info["product_id"]
    serial = (info.get("serial_number") or "").strip()
    if is_placeholder_serial(serial):
        return f"{vid:04x}:{pid:04x}"
    return f"{vid:04x}:{pid:04x}:{serial}"


def describe_device(info: DeviceInfo) -> str:
    """Best-effort label from the device's own strings, for logs and probe output."""
    product = (info.get("product_string") or "").strip()
    manufacturer = (info.get("manufacturer_string") or "").strip()
    vid = info["vendor_id"]
    pid = info["product_id"]
    if product and manufacturer and manufacturer not in product:
        return f"{manufacturer} {product}"
    return product or manufacturer or f"{vid:04x}:{pid:04x}"


def matches(
    info: DeviceInfo,
    *,
    vendor_id: int | None = None,
    product_ids: Iterable[int] | None = None,
    usage_page: int | None = None,
    usage: int | None = None,
    interface_number: int | None = None,
) -> bool:
    """Match a collection on the full identifying tuple.

    Any argument left as None is not constrained. Drivers should always pin
    down usage_page (and usually usage) so they select the vendor collection
    rather than the mouse collection.
    """
    if vendor_id is not None and info.get("vendor_id") != vendor_id:
        return False
    if product_ids is not None and info.get("product_id") not in product_ids:
        return False
    if usage_page is not None and info.get("usage_page") != usage_page:
        return False
    if usage is not None and info.get("usage") != usage:
        return False
    if interface_number is not None and info.get("interface_number") != interface_number:
        return False
    return True


def reading_rank(reading: Reading) -> int:
    """How useful a reading is when two of them describe the same mouse."""
    if reading.online and reading.percent is not None:
        return 2
    return 1 if reading.online else 0


def collapse_duplicates(
    results: list[tuple[str, Reading]] | list[tuple[str, str, Reading]],
) -> list:
    """Keep one entry per identity, preferring the reading that answered.

    Both forms of one mouse can be present at once -- leaving the dongle
    plugged in while running wired gives a live wired device plus a dongle
    whose mouse is absent. Without this the entry would flicker between the
    real percentage and "off" depending on iteration order.
    """
    best: dict[str, tuple] = {}
    for item in results:
        key = item[0]
        reading = item[-1]
        current = best.get(key)
        if current is None or reading_rank(reading) > reading_rank(current[-1]):
            best[key] = item
    return list(best.values())


def resolve_identity(driver: "Driver", info: DeviceInfo) -> str:
    """Stable id for the *physical* mouse, not the USB device.

    A mouse that can run wired or on a dongle enumerates under two different
    product ids, which would otherwise show up as two entries that never agree.
    Drivers that know two ids are the same hardware say so via `identity()`;
    everything else falls back to the hardware key.
    """
    getter = getattr(driver, "identity", None)
    if getter is not None:
        try:
            identity = getter(info)
        except Exception:
            identity = None
        if identity:
            return identity
    return device_key(info)


def resolve_label(driver: "Driver", info: DeviceInfo) -> str:
    """Preferred display label, letting a driver override the USB strings.

    Worth overriding: these devices report things like "PIAO-2.4G" wirelessly
    and "BYTECH PIAO" wired, so without this the merged entry's name would flip
    depending on how it happened to be plugged in.
    """
    getter = getattr(driver, "label", None)
    if getter is not None:
        try:
            label = getter(info)
        except Exception:
            label = None
        if label:
            return label
    return describe_device(info)


@runtime_checkable
class Driver(Protocol):
    """Protocol implemented by every per-vendor battery driver.

    `identity()` and `label()` are optional; see `resolve_identity` /
    `resolve_label`.
    """

    name: str
    vendor_ids: frozenset[int]

    def candidates(self, infos: Iterable[DeviceInfo]) -> list[DeviceInfo]:
        """Select the collection(s) this driver can talk to.

        Should return at most one entry per physical device.
        """
        ...

    def read(self, info: DeviceInfo) -> Reading:
        """Query battery for one collection returned by `candidates`.

        Returns `Reading(online=False)` when the device is reachable but the
        mouse is powered off. Raises on transport failure so the poller can
        apply backoff.
        """
        ...


_REGISTRY: list[Driver] = []


def register(driver: Driver) -> Driver:
    """Add a driver to the global registry. Safe to call at import time."""
    _REGISTRY.append(driver)
    return driver


def all_drivers() -> list[Driver]:
    return list(_REGISTRY)
