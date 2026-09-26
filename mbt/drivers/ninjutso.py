"""Ninjutso Sora V2 battery.

Taken from Ninjutso's WebHID configurator (ninjaforce.co/sorav2), a Nuxt app;
the battery query is in the `BiYPX5p0` chunk, the wake check and product id
tables in `BLpTb2ES` and `DnKJfvde`. All on Nordic's vid, which Orbitalworks
shares, so the product ids and usage page are both required to claim a device.

Vendor collection is usage page 0xffa0, feature report 0x05, 31-byte payload:

    payload    = 31 zero bytes
    payload[0] = command
    payload[3] = 0x01
    payload[6] = argument
    send_feature_report([0x05] + payload)
    get_feature_report(0x05) -> 32 bytes, report id kept at [0]

Replies echo the command at [1] and the argument at [7], which is what a reply
is matched on. Offsets below are into the raw hidapi buffer; the vendor's
DataView indices are the same, because WebHID also keeps the report id.

    0x15 (21), arg 4   battery     [9] level %, [10] charging, [12] mouse awake
    0x28 (40), arg 2   paired pid  [9..10] little-endian wired product id

The level byte is a true percentage: the vendor assigns it to the UI as is.
[12] is the vendor's own presence test -- zero raises its "device is asleep"
dialog -- so presence comes from that, not from the reply existing. Byte 31
looks like a checksum but does not always sum, and the vendor never checks it.

A receiver reports the wired product id of the mouse it is paired with, so a
wireless reading carries `ninjutso:<wired pid>` as its device id and the wired
form of the same mouse resolves to the same key.

STATUS: verified on a Sora V2 receiver (1915:ae1c) paired to a black Sora V2
(ae11): level 85 matching the vendor configurator, charging 0, awake 1, paired
pid 0xae11. The wired ids, the 8K receivers, the charging flag reading 1 and
the awake flag reading 0 are from the bundle only.
"""

from __future__ import annotations

import time
from dataclasses import replace

from .. import hidio
from .base import DeviceInfo, Reading, matches, register

VENDOR_NORDIC = 0x1915

VENDOR_USAGE_PAGE = 0xFFA0
REPORT_ID = 0x05
PAYLOAD_LEN = 31

CMD_BATTERY = 0x15
ARG_BATTERY = 0x04
CMD_PAIRED_PID = 0x28
ARG_PAIRED_PID = 0x02

COMMAND_OFFSET = 1
ARGUMENT_OFFSET = 7
LEVEL_OFFSET = 9
CHARGING_OFFSET = 10
AWAKE_OFFSET = 12
PID_OFFSET = 9

# The vendor waits 15 ms either side of the exchange and tries the wake check
# three times before telling the user the mouse is asleep.
REPLY_DELAY = 0.015
ATTEMPTS = 3

# The vendor's soraV2 device table. Wired ids come in colour pairs: ae11-ae13
# black/white/pink, ae14-ae16 the same colours on the 3950 sensor.
RECEIVER_PIDS = frozenset({0xAE1C, 0xAE8C, 0xAE8A})
WIRED_PIDS = frozenset({0xAE11, 0xAE12, 0xAE13, 0xAE14, 0xAE15, 0xAE16})
PIDS = RECEIVER_PIDS | WIRED_PIDS


def build_command(command: int, argument: int) -> bytes:
    payload = bytearray(PAYLOAD_LEN)
    payload[0] = command
    payload[3] = 0x01
    payload[6] = argument
    return bytes([REPORT_ID]) + bytes(payload)


def is_reply_to(response: bytes, command: int, argument: int) -> bool:
    """Match on the echoed command and argument, never on arrival order."""
    return (
        len(response) > AWAKE_OFFSET
        and response[0] == REPORT_ID
        and response[COMMAND_OFFSET] == command
        and response[ARGUMENT_OFFSET] == argument
    )


def parse_battery(response: bytes, *, wired: bool = False) -> Reading | None:
    """A battery reply, or None if it is not one we understand."""
    if not is_reply_to(response, CMD_BATTERY, ARG_BATTERY):
        return None
    if not response[AWAKE_OFFSET]:
        return Reading(online=False)
    level = response[LEVEL_OFFSET]
    if level > 100:
        return None
    return Reading(
        online=True,
        percent=level,
        # The vendor's own config parse shows a wired Sora as charging
        # regardless of the flag.
        charging=wired or response[CHARGING_OFFSET] == 1,
        connection="wired" if wired else "2.4 GHz",
    )


def parse_paired_pid(response: bytes) -> int | None:
    if not is_reply_to(response, CMD_PAIRED_PID, ARG_PAIRED_PID):
        return None
    pid = response[PID_OFFSET] | response[PID_OFFSET + 1] << 8
    return pid or None


def identity_for(product_id: int) -> str:
    return f"ninjutso:{product_id:04x}"


class NinjutsoDriver:
    name = "ninjutso"
    vendor_ids = frozenset({VENDOR_NORDIC})

    def identity(self, info: DeviceInfo) -> str | None:
        """Wired ids name the mouse; a receiver's comes from its pairing."""
        pid = info.get("product_id")
        return identity_for(pid) if pid in WIRED_PIDS else None

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """The 0xffa0 command collection, one per physical device."""
        seen: dict[tuple, DeviceInfo] = {}
        for info in infos:
            if not matches(
                info,
                vendor_id=VENDOR_NORDIC,
                product_ids=PIDS,
                usage_page=VENDOR_USAGE_PAGE,
            ):
                continue
            seen.setdefault(
                (info["vendor_id"], info["product_id"], info.get("serial_number") or ""),
                info,
            )
        return list(seen.values())

    def _exchange(self, dev, command: int, argument: int) -> bytes:
        time.sleep(REPLY_DELAY)
        dev.send_feature_report(build_command(command, argument))
        time.sleep(REPLY_DELAY)
        return bytes(dev.get_feature_report(REPORT_ID, PAYLOAD_LEN + 1))

    def read(self, info: DeviceInfo) -> Reading:
        wired = info.get("product_id") in WIRED_PIDS
        with hidio.open_path(info["path"]) as dev:
            device_id = None
            if not wired:
                try:
                    paired = parse_paired_pid(
                        self._exchange(dev, CMD_PAIRED_PID, ARG_PAIRED_PID)
                    )
                except Exception:
                    paired = None
                if paired is not None:
                    device_id = identity_for(paired)

            reading = None
            for _ in range(ATTEMPTS):
                parsed = parse_battery(
                    self._exchange(dev, CMD_BATTERY, ARG_BATTERY), wired=wired
                )
                if parsed is not None:
                    reading = parsed
                    if parsed.online:
                        break

        if reading is None:
            raise hidio.HidError("no battery reply from Ninjutso device")
        if device_id is None:
            return reading
        return replace(reading, device_id=device_id)


register(NinjutsoDriver())
