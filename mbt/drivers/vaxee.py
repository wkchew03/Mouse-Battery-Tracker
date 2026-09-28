"""VAXEE battery protocol.

Taken from the VAXEE Control Center web driver (https://vcc.vaxee.co/), whose
sources ship unminified and commented -- `paramter.js` holds the frame
constants and `api.js` documents each command id.

Vendor collection is usage page 0xff05, feature report 0x0e, 63-byte payload.

    payload    = 63 zero bytes
    payload[0] = 0xA5   header
    payload[1] = cmd    0x0B battery level, 0x10 charging status
    payload[2] = 0x01   read
    payload[3] = 0x01   data length (fixed)
    send_feature_report([0x0e] + payload)
    reply is accepted when byte 1 is non-zero (the vendor's own check)

The level is **not** a percentage: it is a 0-20 step, so 19 means 95%. The web
driver clamps it to 20 and looks the value up in tables keyed _0.._100 in steps
of five, which is what pins the scale.

STATUS: verified on a VAXEE XE-S Wireless (3057:2001), reading 95% at raw 19.
"""

from __future__ import annotations

import time

from .. import hidio
from .base import OFFLINE, DeviceInfo, Reading, matches, register

VENDOR_VAXEE = 0x3057

VENDOR_USAGE_PAGE = 0xFF05
REPORT_ID = 0x0E
PAYLOAD_LEN = 63

HEADER = 0xA5
READ = 0x01
DATA_LENGTH = 0x01

CMD_BATTERY_LEVEL = 0x0B
CMD_CHARGING_STATUS = 0x10

# Battery arrives as a 0-20 step rather than a percentage.
LEVEL_OFFSET = 5
LEVEL_MAX = 20
PERCENT_PER_STEP = 5

REPLY_DELAY = 0.08

# Product ids listed by the web driver's DeviceProductId array.
PIDS = (
    0x0005, 0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1006, 0x1007, 0x1008,
    0x1009, 0x1010, 0x1011, 0x1012, 0x1013, 0x1014, 0x1015, 0x2001, 0x2002,
)


def build_command(command: int) -> bytes:
    payload = bytearray(PAYLOAD_LEN)
    payload[0] = HEADER
    payload[1] = command
    payload[2] = READ
    payload[3] = DATA_LENGTH
    return bytes(payload)


def is_valid_reply(response: bytes, command: int) -> bool:
    """The vendor treats a non-zero byte 1 as success; also check the echo."""
    if len(response) <= LEVEL_OFFSET:
        return False
    return response[1] != 0 and response[2] == command


def parse_level(response: bytes) -> int | None:
    """0-20 step -> percentage, or None if the value is out of range."""
    if len(response) <= LEVEL_OFFSET:
        return None
    raw = response[LEVEL_OFFSET]
    if raw > LEVEL_MAX:
        # The web driver clamps rather than rejecting, but a value above the
        # scale means the reply is not what we think it is.
        return None
    return raw * PERCENT_PER_STEP


class VaxeeDriver:
    name = "vaxee"

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """The 0xff05 command collection, one per physical device."""
        seen: dict[tuple, DeviceInfo] = {}
        for info in infos:
            if not matches(
                info,
                vendor_id=VENDOR_VAXEE,
                product_ids=PIDS,
                usage_page=VENDOR_USAGE_PAGE,
            ):
                continue
            seen.setdefault(
                (info["vendor_id"], info["product_id"], info.get("serial_number") or ""),
                info,
            )
        return list(seen.values())

    def _query(self, dev, command: int) -> bytes | None:
        dev.send_feature_report(bytes([REPORT_ID]) + build_command(command))
        time.sleep(REPLY_DELAY)
        response = bytes(dev.get_feature_report(REPORT_ID, PAYLOAD_LEN + 1))
        return response if is_valid_reply(response, command) else None

    def read(self, info: DeviceInfo) -> Reading:
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)

            level_reply = self._query(dev, CMD_BATTERY_LEVEL)
            if level_reply is None:
                return OFFLINE
            percent = parse_level(level_reply)
            if percent is None:
                return OFFLINE

            charging = None
            try:
                charge_reply = self._query(dev, CMD_CHARGING_STATUS)
                if charge_reply is not None:
                    charging = bool(charge_reply[LEVEL_OFFSET])
            except Exception:
                pass

        return Reading(online=True, percent=percent, charging=charging)


register(VaxeeDriver())
