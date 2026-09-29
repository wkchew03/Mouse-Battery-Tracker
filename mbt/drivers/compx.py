"""CompX generation-2 battery protocol (Attack Shark R6/R8/R5/M5, VID 0x373E).

64-byte command buffers sent as feature reports on report ID 0, no checksum.
Battery is opcode 0x83; the reply is marked by 0xA1 and carries the percentage
and charging flag in adjacent bytes whose order varies by firmware.

Protocol facts (opcode, marker, byte offsets, the swapped byte order) from
attack-shark-r6-cli (`r6ctl.py`, https://github.com/mohammed-just/attack-shark-r6-cli).
No code is taken from it: the parsing below was rewritten from those facts
alone, so this module carries the project's license, not that one's GPL-2.0.

Note this does NOT cover the IPI Float 88 (`372e:1014`), which is a different
platform entirely -- it uses report ID 0x03 with a checksummed `0x50` frame and
never answers this command. That one is implemented in `ipi.py`.

STATUS: verified on a CRDRAKO KO-ONE 8K receiver (373e:006b), cross-checked
against the vendor's panel at 100%. The LAMZU Maya X 8K dongle (373e:001e)
has also been read (73%). Its wired pid 001c is only known from one stored
record, and the two were merged by identity without being seen side by side.
The G-Wolves HTX Ultra has been read as its 8K receiver (33e4:5617, 82%)
and wired while charging (33e4:5608, 91%), and the two are merged the same way.

Note the command channel is not always on usage page 0xff00 -- the KO-ONE uses
0xffff -- so the collection is chosen by which one declares a feature report.
"""

from __future__ import annotations

import time

from .. import hidio, hidparse
from .base import OFFLINE, DeviceInfo, Reading, device_key, matches, register

VENDOR_ATTACK_SHARK = 0x373E
VENDOR_GWOLVES = 0x33E4

# Same firmware platform under different vendor ids: identical collection
# layout (0xffa0/0xffff inputs plus a 64-byte feature channel) and the same
# 0xA1-marked replies.
VENDOR_IDS = frozenset({VENDOR_ATTACK_SHARK, VENDOR_GWOLVES})

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
    0x001C: "LAMZU Maya X (wired)",
    0x001E: "LAMZU Maya X 8K dongle",
}

# One mouse, two USB devices with different serials: the Maya X on its cable
# and its 8K dongle. Without a shared identity it shows as two entries, one of
# which is always "off". Like ipi.py this merges by model, so two Maya Xs would
# share an entry.
MODEL_GROUPS = {
    (VENDOR_ATTACK_SHARK, 0x001C): "lamzu:mayax",
    (VENDOR_ATTACK_SHARK, 0x001E): "lamzu:mayax",
    # The mouse itself enumerates as 5608 on its charging cable.
    (VENDOR_GWOLVES, 0x5608): "gwolves:htxultra",
    (VENDOR_GWOLVES, 0x5617): "gwolves:htxultra",
}
MODEL_NAMES = {"lamzu:mayax": "LAMZU Maya X", "gwolves:htxultra": "G-Wolves HTX Ultra"}

# Hardware keys seen this session -> their group. The old keys carry the
# serial, so unlike ipi.py's table they cannot be listed ahead of time.
_seen_keys: dict[str, str] = {}


def _group(info: DeviceInfo) -> str | None:
    return MODEL_GROUPS.get((info.get("vendor_id"), info.get("product_id")))


def legacy_aliases() -> dict[str, str]:
    """Serial-keyed records from before the merge -> the merged identity."""
    return dict(_seen_keys)

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
    """Split the two data bytes into (percent, charging).

    Firmware puts the 0/1 charging flag on either side of the percentage. A
    flag in the first byte wins when both readings are plausible. With no 0/1 byte
    the charging state is unknown and the first byte that could be a
    percentage is used.
    """
    for flag, level in ((a, b), (b, a)):
        if flag in (0, 1) and level <= 100:
            return level, bool(flag)
    level = next((byte for byte in (a, b) if byte <= 100), None)
    return level, None


# The reply's layout: marker, two bytes, the 0x02 status, a byte, the echoed
# opcode, then the two data bytes. Depending on the host stack the report id
# is left at the front of the buffer or stripped, so the frame starts at 1 or 0.
FRAME_STATUS = 3
FRAME_OPCODE = 5
FRAME_DATA = 6


def _battery_frame(response: bytes) -> bytes | None:
    for start in (1, 0):
        frame = response[start:]
        if (frame[0] == RESPONSE_MARKER and frame[FRAME_STATUS] == 2
                and frame[FRAME_OPCODE] == OPCODE_BATTERY):
            return frame
    return None


def parse_battery(response: bytes) -> Reading:
    # Nine bytes covers a frame with the report id still in front of it.
    frame = _battery_frame(response) if len(response) >= 9 else None
    percent, charging = (normalize_battery(frame[FRAME_DATA], frame[FRAME_DATA + 1])
                         if frame is not None else (None, None))
    if percent is None:
        return OFFLINE
    return Reading(online=True, percent=percent, charging=charging)


class CompxDriver:
    name = "compx-gen2"

    def identity(self, info: DeviceInfo) -> str | None:
        group = _group(info)
        if group:
            _seen_keys[device_key(info)] = group
        return group

    def label(self, info: DeviceInfo) -> str | None:
        group = _group(info)
        return MODEL_NAMES.get(group) if group else None

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Pick the collection that carries the command channel.

        Selection is driven by the presence of a feature report rather than a
        fixed usage page: the reference implementation scores 0xff00, but the
        CRDRAKO KO-ONE puts the same protocol on 0xffff, and requiring 0xff00
        silently skipped the device entirely.
        """
        best: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if (info.get("vendor_id") or 0) not in VENDOR_IDS:
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
