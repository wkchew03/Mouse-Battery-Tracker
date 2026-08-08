"""CompX generation-2 battery protocol (Attack Shark R6/R8/R5/M5, VID 0x373E).

64-byte command buffers sent as feature reports on report ID 0, no checksum.
Battery is opcode 0x83; the reply is marked by 0xA1 and carries the percentage
and charging flag in adjacent bytes whose order varies by firmware.

Protocol from attack-shark-r6-cli (`r6ctl.py`).

Note this does NOT cover the IPI Float 88 (`372e:1014`), which is a different
platform entirely -- it uses report ID 0x03 with a checksummed `0x50` frame and
never answers this command. That one is implemented in `ipi.py`.

STATUS: verified on a CRDRAKO KO-ONE 8K receiver (373e:006b), cross-checked
against the vendor's panel at 100%.

Note the command channel is not always on usage page 0xff00 -- the KO-ONE uses
0xffff -- so the collection is chosen by which one declares a feature report.
"""

from __future__ import annotations

import time

from .. import hidio, hidparse
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
    0x006B: "CRDRAKO KO-ONE 8K receiver",
}

# Cache of "does this collection declare a feature report", keyed by HID path.
# Answering it means opening the device to read its descriptor, and candidates()
# runs on every poll.
_feature_channel_cache: dict[bytes, bool] = {}


def has_feature_channel(info: DeviceInfo) -> bool:
    """True when this collection declares a feature report.

    This is what actually distinguishes the command channel. The usage page
    varies across the platform -- Attack Shark uses 0xff00, the CRDRAKO KO-ONE
    uses 0xffff -- but only the command collection has feature reports; the
    others carry input-only telemetry.
    """
    path = info.get("path")
    if path is None:
        return False
    cached = _feature_channel_cache.get(path)
    if cached is not None:
        return cached

    try:
        descriptor = hidparse.parse(hidio.report_descriptor(path))
        result = any(sizes.feature for sizes in descriptor.reports.values())
    except Exception:
        result = False
    _feature_channel_cache[path] = result
    return result


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
        """Pick the collection that carries the command channel.

        Selection is driven by the presence of a feature report rather than a
        fixed usage page: the reference implementation scores 0xff00, but the
        CRDRAKO KO-ONE puts the same protocol on 0xffff, and requiring 0xff00
        silently skipped the device entirely.
        """
        best: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if not matches(info, vendor_id=VENDOR_ATTACK_SHARK):
                continue
            usage_page = info.get("usage_page") or 0
            if not 0xFF00 <= usage_page <= 0xFFFF:
                continue

            score = 0
            if has_feature_channel(info):
                score += 100
            if usage_page == 0xFF00:
                score += 20
            if (info.get("usage") or 0) == 0x01:
                score += 5
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
