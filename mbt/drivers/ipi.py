"""IPI mouse battery protocol (`ms_pix_v1`), e.g. the IPI Float 88.

Extracted from IPI's official WebHID configurator at https://shan.ipigame.cn/
and verified against hardware. See docs/protocols.md for the full derivation.

Battery is not a dedicated call -- it is field 5 of `get_basic_info`:

    payload    = [crc, 0x50, 0x00, 0x02, 0x4F, 0x81] padded to 63 bytes
    payload[0] = sum(payload[1:]) % 256
    send_feature_report([report_id] + payload)
    read back until the reply is longer than the 5-byte stub
    battery = response[6]        (report ID at [0], vendor's a[5] == raw[6])

Two behaviours here are easy to get wrong and both are load-bearing:

1. The device answers commands but *never* volunteers battery. Its unsolicited
   0xFA notifications carry only link state, DPI and report rate -- the type
   0x05 report is discarded by IPI's own driver, so it must not be read as a
   battery value.
2. The first feature read after sending returns a 5-byte status stub. The real
   payload only appears on a subsequent read, which is why the vendor retries.
   Reading once and giving up looks exactly like an unresponsive device.
"""

from __future__ import annotations

import time
from dataclasses import replace

from .. import hidio
from .base import OFFLINE, DeviceInfo, Reading, matches, register

VENDOR_IPI = 0x372E

# "Mouse PIAO" entries from the configurator's device table.
PIDS = {
    0x1014: "Float 88 (wireless)",
    0x1015: "Float 88 (wired)",
    0x1028: "PIAO (wired)",
    0x1056: "PIAO (wired)",
}

# Which product ids are the same piece of hardware. 0x1014 and 0x1015 were
# confirmed to be one mouse by watching it re-enumerate when the cable went in.
# 0x1028 and 0x1056 come from the vendor table and are kept separate, because
# nothing has been verified about them.
MODEL_GROUPS = {
    0x1014: "ipi:float88",
    0x1015: "ipi:float88",
    0x1028: "ipi:piao-1028",
    0x1056: "ipi:piao-1056",
}

MODEL_NAMES = {
    "ipi:float88": "IPI Float 88",
    "ipi:piao-1028": "IPI PIAO",
    "ipi:piao-1056": "IPI PIAO",
}

# Reported alongside the reading so a merged entry still shows how it is attached.
CONNECTIONS = {
    0x1014: "2.4 GHz",
    0x1015: "wired",
    0x1028: "wired",
    0x1056: "wired",
}

VENDOR_USAGE_PAGE = 0xFF00
REPORT_ID = 0x03
PAYLOAD_LEN = 63
FRAME_MARKER = 0x50

# get_basic_info: [crc, 0x50, 0x00, <len>, <opcode>, <param>]
CMD_BASIC_INFO = [0x00, FRAME_MARKER, 0x00, 0x02, 0x4F, 0x81]

# Value the battery field carries while the mouse is on external power.
#
# It is a sentinel, not an encoded level: the same 0xE4 was observed while the
# mouse sat wired at roughly 80-85%, so it cannot be `0x80 | percent` (that
# would have read 0xC4). Confirmed against hardware while charging, where it
# stayed at exactly 228 across repeated polls.
CHARGING_SENTINEL = 0xE4

# The stub reply is 5 bytes; anything longer is a real payload.
STUB_LEN = 5
READ_ATTEMPTS = 8
READ_INTERVAL = 0.05


def set_crc(payload: list[int]) -> list[int]:
    """Store `sum(payload[1:]) % 256` in payload[0] (the vendor's `_set_crc`)."""
    payload = list(payload)
    payload[0] = sum(payload[1:]) % 256
    return payload


def build_command(command: list[int]) -> bytes:
    """Pad a command to the 63-byte payload and apply the checksum."""
    payload = list(command) + [0] * (PAYLOAD_LEN - len(command))
    return bytes(set_crc(payload))


def legacy_aliases() -> dict[str, str]:
    """Old per-device keys mapped to their merged identity.

    Lets stored history from before entries were merged fold into the new key
    instead of lingering as a stale duplicate under "Recently used".
    """
    return {
        f"{VENDOR_IPI:04x}:{pid:04x}": group for pid, group in MODEL_GROUPS.items()
    }


def parse_basic_info(response: bytes) -> Reading:
    """Parse a get_basic_info reply.

    Indices are relative to the raw hidapi buffer, which keeps the report ID at
    [0]; the vendor's code drops it first, so their a[n] is our raw[n + 1].
    """
    if len(response) <= STUB_LEN:
        return OFFLINE
    if response[1] != FRAME_MARKER or response[4] != CMD_BASIC_INFO[3]:
        return OFFLINE

    percent = response[6]
    if percent == CHARGING_SENTINEL:
        # On external power. No percentage is available in this state; callers
        # fall back to the last stored level rather than inventing one.
        return Reading(online=True, charging=True)
    if 0 <= percent <= 100:
        return Reading(online=True, percent=percent)
    # The frame validated, so the device is definitely answering -- it just did
    # not give a usable level. Report it as present with an unknown level
    # rather than as powered off.
    return Reading(online=True)


class IpiDriver:
    name = "ipi"

    def identity(self, info: DeviceInfo) -> str | None:
        """Same id for the wired and wireless forms of one mouse."""
        return MODEL_GROUPS.get(info.get("product_id"))

    def label(self, info: DeviceInfo) -> str | None:
        """Model name, so the entry doesn't rename itself when you plug in."""
        group = MODEL_GROUPS.get(info.get("product_id"))
        return MODEL_NAMES.get(group) if group else None

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Select the 0xFF00 vendor collection, one per physical device."""
        seen: dict[tuple, DeviceInfo] = {}
        for info in infos:
            if not matches(
                info,
                vendor_id=VENDOR_IPI,
                product_ids=PIDS,
                usage_page=VENDOR_USAGE_PAGE,
            ):
                continue
            ident = (info["vendor_id"], info["product_id"])
            seen.setdefault(ident, info)
        return list(seen.values())

    def read(self, info: DeviceInfo) -> Reading:
        command = build_command(CMD_BASIC_INFO)
        connection = CONNECTIONS.get(info.get("product_id"))
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)
            dev.send_feature_report(bytes([REPORT_ID]) + command)

            # The first read returns a short stub; keep reading for the payload.
            for _ in range(READ_ATTEMPTS):
                time.sleep(READ_INTERVAL)
                response = bytes(dev.get_feature_report(REPORT_ID, PAYLOAD_LEN + 1))
                if len(response) > STUB_LEN:
                    reading = parse_basic_info(response)
                    if reading.online and connection:
                        return replace(reading, connection=connection)
                    return reading
        return OFFLINE


register(IpiDriver())
