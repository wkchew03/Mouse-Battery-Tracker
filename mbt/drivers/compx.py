"""CompX generation-2 battery protocol (Attack Shark R6/R8/R5/M5, VID 0x373E).

64-byte command buffers sent as feature reports on report ID 0, no checksum.
Battery is opcode 0x83; the reply is marked by 0xA1 and carries the percentage
and charging flag in adjacent bytes whose order varies by firmware.

Protocol from attack-shark-r6-cli (`r6ctl.py`).

Note this does NOT cover the IPI Float 88 (`372e:1014`), which is a different
platform entirely -- it uses report ID 0x03 with a checksummed `0x50` frame and
never answers this command. That one is implemented in `ipi.py`.

STATUS: unit-tested against the documented layout; no CompX gen-2 hardware was
available to confirm it.
"""

from __future__ import annotations

import time

from .. import hidio
from .base import OFFLINE, DeviceInfo, Reading, matches, register

VENDOR_ATTACK_SHARK = 0x373E

REPORT_ID = 0x00
BUFFER_LEN = 64
RESPONSE_MARKER = 0xA1
OPCODE_BATTERY = 131  # 0x83
DELAY = 0.05
RETRIES = 3

PIDS = {
    0x0021: "R6 (wired)",
    0x0022: "R6 (wireless)",
    0x003A: "R8 (wired)",
    0x003B: "R8 (wireless)",
    0x0046: "R5 Ultra (wired)",
    0x0047: "R5 Ultra (wireless)",
    0x0050: "M5 Ultra (wireless)",
    0x0051: "M5 Ultra (wired)",
}


def build_command(b2: int, b3: int, b4: int = 0, b5: int = 0) -> bytes:
    """64-byte command buffer with the opcode fields at indices 2..5."""
    buffer = bytearray(BUFFER_LEN)
    buffer[2] = b2
    buffer[3] = b3
    buffer[4] = b4
    buffer[5] = b5
    return bytes(buffer)


def normalize_battery(a: int, b: int) -> tuple[int | None, bool | None]:
    """Firmware disagrees on the order of (percent, charging_flag).

    The byte in 0..1 is the flag and the one in 0..100 is the percentage; when
    both are ambiguous, prefer the first plausible percentage.
    """
    if a in (0, 1) and 0 <= b <= 100:
        return b, bool(a)
    if b in (0, 1) and 0 <= a <= 100:
        return a, bool(b)
    if 0 <= a <= 100:
        return a, None
    if 0 <= b <= 100:
        return b, None
    return None, None


def parse_battery(response: bytes) -> Reading:
    """Response carries 0xA1 at index 1, then the echoed opcode at index 6."""
    if len(response) < 9:
        return OFFLINE
    if response[1] == RESPONSE_MARKER and response[4] == 2 and response[6] == OPCODE_BATTERY:
        percent, charging = normalize_battery(response[7], response[8])
    elif response[0] == RESPONSE_MARKER and response[3] == 2 and response[5] == OPCODE_BATTERY:
        percent, charging = normalize_battery(response[6], response[7])
    else:
        return OFFLINE

    if percent is None:
        return OFFLINE
    return Reading(online=True, percent=percent, charging=charging)


class CompxDriver:
    name = "compx-gen2"
    vendor_ids = frozenset({VENDOR_ATTACK_SHARK})

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Score by usage page 0xFF00 (+100), usage 0x01 (+50), low interface."""
        best: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if not matches(info, vendor_id=VENDOR_ATTACK_SHARK):
                continue
            usage_page = info.get("usage_page") or 0
            usage = info.get("usage") or 0
            interface = info.get("interface_number")
            interface = 99 if interface is None else interface

            score = 0
            if usage_page == 0xFF00:
                score += 100
            if usage == 0x01:
                score += 50
            score += max(0, 10 - min(interface, 10))
            if score < 100:
                continue

            ident = (info["vendor_id"], info["product_id"], info.get("serial_number") or "")
            current = best.get(ident)
            if current is None or score > current[0]:
                best[ident] = (score, info)
        return [info for _, info in best.values()]

    def read(self, info: DeviceInfo) -> Reading:
        command = build_command(2, 2, 0, OPCODE_BATTERY)
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)
            dev.send_feature_report(bytes([REPORT_ID]) + command)
            time.sleep(DELAY)
            for _ in range(RETRIES):
                response = bytes(dev.get_feature_report(REPORT_ID, BUFFER_LEN + 1))
                if len(response) > 1 and response[1] == RESPONSE_MARKER:
                    return parse_battery(response)
                time.sleep(DELAY)
            return parse_battery(response)


register(CompxDriver())
