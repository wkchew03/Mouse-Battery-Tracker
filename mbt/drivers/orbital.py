"""Orbitalworks battery protocol (Pathfinder, Ghost).

Taken from the official WebHID configurator at https://orbital-web-ctrl.pages.dev/,
whose source ships unminified -- the constants below are the vendor's own.

Vendor collection is usage page 0xff0a / usage 0x01, with unnumbered 64-byte
input and output reports.

    packet     = 64 zero bytes
    packet[0]  = 0x01, OR'd with 0x40 when talking through a receiver
    packet[2]  = 0x81   (read power)
    packet[3]  = 0x01
    packet[63] = (161 - sum(packet[0:63])) & 0xff

Reply is accepted when packet[0] is 0x01 or 0x41 and packet[3] is 0x01. Field
offsets depend on the protocol generation, which the vendor keys off product id:

    dms     state=[5]  value=[6]  profile=[7]
    dms_v2  state=[10] value=[11] profile=[12]

Power state: 1 or 3 = charging, 2 = fully charged, anything else = on battery.

STATUS: verified on a Pathfinder V1 receiver (1915:0746).
"""

from __future__ import annotations

import time

from .. import hidio
from .base import OFFLINE, DeviceInfo, Reading, Reading as _Reading, matches, register

VENDOR_ORBITAL = 0x1915

PACKET_LEN = 64
CHECKSUM_BASE = 161
RECEIVER_FLAG = 0x40

CMD_READ_POWER = 0x81

VENDOR_USAGE_PAGE = 0xFF0A
VENDOR_USAGE = 0x01

RESPONSE_TIMEOUT = 1.5
READ_TIMEOUT_MS = 300

# The vendor's ORBITAL_DEVICES table.
DEVICES = {
    0x0746: {"name": "Pathfinder V1", "receiver": True, "protocol": "dms"},
    0x0747: {"name": "Pathfinder V1", "receiver": False, "protocol": "dms"},
    0x080B: {"name": "Ghost", "receiver": True, "protocol": "dms_v2"},
    0x080C: {"name": "Ghost", "receiver": False, "protocol": "dms_v2"},
}

# Receiver and wired ids are the same physical mouse (the vendor pairs them via
# `pairedProductId`), so they share one entry.
MODEL_GROUPS = {
    0x0746: "orbital:pathfinder",
    0x0747: "orbital:pathfinder",
    0x080B: "orbital:ghost",
    0x080C: "orbital:ghost",
}

MODEL_NAMES = {
    "orbital:pathfinder": "Orbital Pathfinder",
    "orbital:ghost": "Orbital Ghost",
}

OFFSETS = {
    "dms": {"state": 5, "value": 6},
    "dms_v2": {"state": 10, "value": 11},
}

# Command marker the device puts at byte 2 of a power reply, per the vendor's
# own handleDeviceReport. Checking it is essential, not decorative: the
# receiver answers an unrouted request with a *different* message whose byte 2
# is 0x8c, and that reply satisfies every other condition -- so without this it
# parses as a plausible but entirely wrong percentage.
POWER_MARKERS = {
    "dms": (0x8E,),
    "dms_v2": (0x98, 0x99),
}

STATE_CHARGING = (1, 3)
STATE_FULL = 2


def checksum(packet: bytes) -> int:
    """Trailing byte, chosen so the packet reconciles to the vendor's constant."""
    return (CHECKSUM_BASE - (sum(packet[:63]) & 0xFF)) & 0xFF


def build_packet(command: int, receiver: bool) -> bytes:
    packet = bytearray(PACKET_LEN)
    packet[0] = 0x01 | (RECEIVER_FLAG if receiver else 0x00)
    packet[2] = command
    packet[3] = 0x01
    packet[63] = checksum(bytes(packet))
    return bytes(packet)


def is_power_reply(response: bytes, protocol: str = "dms") -> bool:
    if len(response) < PACKET_LEN:
        return False
    if response[0] not in (0x01, 0x41) or response[3] != 0x01:
        return False
    return response[2] in POWER_MARKERS.get(protocol, POWER_MARKERS["dms"])


def parse_power(response: bytes, protocol: str) -> Reading:
    """Decode a read-power reply into a Reading."""
    offsets = OFFSETS.get(protocol, OFFSETS["dms"])
    if len(response) <= offsets["value"]:
        return OFFLINE

    state = response[offsets["state"]]
    percent = response[offsets["value"]]
    if not 0 <= percent <= 100:
        return OFFLINE

    charging = state in STATE_CHARGING or state == STATE_FULL
    return _Reading(online=True, percent=percent, charging=charging)


class OrbitalDriver:
    name = "orbital"
    vendor_ids = frozenset({VENDOR_ORBITAL})

    def identity(self, info: DeviceInfo) -> str | None:
        return MODEL_GROUPS.get(info.get("product_id"))

    def label(self, info: DeviceInfo) -> str | None:
        group = MODEL_GROUPS.get(info.get("product_id"))
        return MODEL_NAMES.get(group) if group else None

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """The single vendor collection, one per physical device."""
        seen: dict[tuple, DeviceInfo] = {}
        for info in infos:
            if not matches(
                info,
                vendor_id=VENDOR_ORBITAL,
                product_ids=DEVICES,
                usage_page=VENDOR_USAGE_PAGE,
                usage=VENDOR_USAGE,
            ):
                continue
            seen.setdefault((info["vendor_id"], info["product_id"]), info)
        return list(seen.values())

    def read(self, info: DeviceInfo) -> Reading:
        definition = DEVICES.get(info.get("product_id"))
        if definition is None:
            return OFFLINE

        packet = build_packet(CMD_READ_POWER, definition["receiver"])
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)
            # Unnumbered reports: hidapi still wants a leading report-id byte,
            # which it strips before sending.
            dev.write(b"\x00" + packet)

            deadline = time.time() + RESPONSE_TIMEOUT
            while time.time() < deadline:
                response = bytes(dev.read(PACKET_LEN, READ_TIMEOUT_MS))
                if is_power_reply(response, definition["protocol"]):
                    reading = parse_power(response, definition["protocol"])
                    if reading.online:
                        connection = "2.4 GHz" if definition["receiver"] else "wired"
                        return _Reading(
                            online=True,
                            percent=reading.percent,
                            charging=reading.charging,
                            connection=connection,
                        )
                    return reading
        return OFFLINE


register(OrbitalDriver())
