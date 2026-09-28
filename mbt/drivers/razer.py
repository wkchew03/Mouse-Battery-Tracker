"""Razer battery protocol.

90-byte reports sent as HID feature reports, with an XOR checksum over bytes
2..87. Battery is command class 0x07, command id 0x80.

The awkward part is `transaction_id`, which differs per model (0x1f, 0x3f, 0x08,
0x00 are all in use). A wrong value does not raise -- the device simply returns
a report that fails validation, or worse, plausible-looking garbage. Rather than
shipping a partial per-PID table copied from OpenRazer and silently misreporting
on anything missing from it, this driver sweeps the known ids, validates the
echoed command class/id, and caches whichever one works per device.

Sweeping is safe here: 0x07/0x80 is a read command, so a mismatched transaction
id is simply ignored by the device.

STATUS: verified on a Viper V3 Pro dongle (1532:00c1), cross-checked against
Razer Synapse (both reported 100%). The level is confirmed to be a 0-255 value
scaled to a percentage, and it tracks real change (128 -> 255 across a charge).

The frame layout, CRC and argument offsets are corroborated independently: the
same channel returns a real ASCII serial ("PM2440H32017653"), firmware v1.12,
and a correct 800x800 DPI pair.

Two device-specific findings worth keeping:

- This device exposes TWO mouse collections (interface 0 and interface 1) and
  only interface 0 answers; reads on interface 1 raise outright. The scoring in
  `candidates()` picks the lower interface, which is what makes it work.
- The transaction id is echoed, not validated: all four known ids return the
  same valid response. The sweep is therefore harmless but also not meaningful
  on this model.
"""

from __future__ import annotations

import time

from .. import hidio
from .base import (
    OFFLINE,
    VENDOR_RAZER,
    DeviceInfo,
    Reading,
    matches,
    register,
)

REPORT_LEN = 90
REPORT_ID = 0x00

CLASS_POWER = 0x07
CMD_BATTERY_LEVEL = 0x80
CMD_CHARGING_STATUS = 0x84

TRANSACTION_IDS = (0x1F, 0x3F, 0x08, 0x00)

STATUS_SUCCESS = 0x02
STATUS_BUSY = 0x01

# Cache of the transaction id that worked, keyed by (vid, pid).
_transaction_cache: dict[tuple[int, int], int] = {}


def crc(report: bytes) -> int:
    """XOR of bytes 2..87 inclusive."""
    value = 0
    for byte in report[2:88]:
        value ^= byte
    return value


def build_report(
    command_class: int,
    command_id: int,
    data_size: int = 0x02,
    arguments: bytes = b"",
    transaction_id: int = 0x1F,
) -> bytes:
    """Build the 90-byte Razer report.

    Layout: status, transaction_id, remaining_packets(2 BE), protocol_type,
    data_size, command_class, command_id, arguments[80], crc, reserved.
    """
    report = bytearray(REPORT_LEN)
    report[0] = 0x00  # status: 0 on requests
    report[1] = transaction_id
    report[2] = 0x00  # remaining packets, big endian
    report[3] = 0x00
    report[4] = 0x00  # protocol type
    report[5] = data_size
    report[6] = command_class
    report[7] = command_id
    if len(arguments) > 80:
        raise ValueError("arguments exceed the 80-byte field")
    report[8 : 8 + len(arguments)] = arguments
    report[88] = crc(bytes(report))
    report[89] = 0x00
    return bytes(report)


def is_valid_response(response: bytes, command_class: int, command_id: int) -> bool:
    """Check the device actually answered this command."""
    if len(response) < REPORT_LEN:
        return False
    if response[0] not in (STATUS_SUCCESS, STATUS_BUSY):
        return False
    return response[6] == command_class and response[7] == command_id


def parse_battery(response: bytes) -> int | None:
    """Battery level is a 0-255 value in the first argument byte."""
    if len(response) < 10:
        return None
    level = response[9]
    if level == 0:
        return None
    return round(level / 255 * 100)


class RazerDriver:
    name = "razer"

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Pick the control collection for each Razer device.

        Razer's control channel is the Generic Desktop mouse collection
        (usage page 0x0001, usage 0x0002) on the lowest interface. Feature
        reports work there even though Windows blocks reading *input* reports
        from mouse collections.
        """
        best: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if not matches(info, vendor_id=VENDOR_RAZER):
                continue
            usage_page = info.get("usage_page") or 0
            usage = info.get("usage") or 0
            interface = info.get("interface_number")
            interface = 99 if interface is None else interface

            score = 0
            if usage_page == 0x0001 and usage == 0x0002:
                score += 100
            elif 0xFF00 <= usage_page <= 0xFFFF:
                score += 60
            score += max(0, 10 - min(interface, 10))

            ident = (info["vendor_id"], info["product_id"], info.get("serial_number") or "")
            current = best.get(ident)
            if current is None or score > current[0]:
                best[ident] = (score, info)
        return [info for _, info in best.values()]

    def _transact(self, dev, command_class: int, command_id: int, transaction_id: int):
        request = build_report(command_class, command_id, transaction_id=transaction_id)
        dev.send_feature_report(bytes([REPORT_ID]) + request)
        time.sleep(0.06)
        return bytes(dev.get_feature_report(REPORT_ID, REPORT_LEN + 1))[1:]

    def read(self, info: DeviceInfo) -> Reading:
        ident = (info["vendor_id"], info["product_id"])
        order = list(TRANSACTION_IDS)
        cached = _transaction_cache.get(ident)
        if cached is not None:
            order.remove(cached)
            order.insert(0, cached)

        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)
            for transaction_id in order:
                try:
                    response = self._transact(
                        dev, CLASS_POWER, CMD_BATTERY_LEVEL, transaction_id
                    )
                except Exception:
                    continue
                if not is_valid_response(response, CLASS_POWER, CMD_BATTERY_LEVEL):
                    continue

                percent = parse_battery(response)
                if percent is None:
                    # Valid reply, zero level: the mouse is asleep or off.
                    _transaction_cache[ident] = transaction_id
                    return OFFLINE

                charging = None
                try:
                    charge_response = self._transact(
                        dev, CLASS_POWER, CMD_CHARGING_STATUS, transaction_id
                    )
                    if is_valid_response(charge_response, CLASS_POWER, CMD_CHARGING_STATUS):
                        charging = bool(charge_response[9])
                except Exception:
                    pass

                _transaction_cache[ident] = transaction_id
                return Reading(online=True, percent=percent, charging=charging)

        return OFFLINE


register(RazerDriver())
