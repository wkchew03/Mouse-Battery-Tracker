"""Finalmouse UltralightX battery.

64-byte reports on a vendor collection (usage page 0xff00): report 0x04 out,
0x05 in for the ULX, 0x01 out for the SLX. The frame is three header bytes and
then arguments:

    [0]   report id (0x04)
    [1]   2 + len(args)        -- total payload length
    [2]   0x80 | command       -- the high bit marks a request
    [3]   len(args)
    [4:]  args, zero padded to 63 bytes

Replies arrive on report 0x05 with the command echoed *without* the high bit,
so a reply is matched on the echoed command rather than by taking the next
report that turns up. That matters here more than on most of these devices:
the dongle continuously volunteers `CMD_ID_RSSI` (0x0d) while the link is up,
several times a second, so a driver that reads the next report gets link
strength and reports it as a battery level.

Taken from Finalmouse's own XPanel configurator (xpanel.finalmouse.com), which
is a WebHID app; its bundle carries the whole command table as a named enum,
`CMD_ID_*`, and parses a battery status reply as `{soc, voltage_mv}` with the
voltage a little-endian 16-bit at offset 1.

The product ids are Finalmouse's own, from their published udev rules
(github.com/teamfinalmouse/xpanel-linux-permissions).

STATUS: verified on an UltralightX dongle (361d:0100). `CMD_ID_VBAT` returned
3956 mV and later 3964, so it tracks the cell rather than being frozen;
`CMD_ID_BATTERY_CHARGING` returned 0; `CMD_ID_ULX_GET_DPI` and
`..._POLLING_RATE` returned 1600 and 2000, both matching the hardware;
`CMD_ID_LINK_STATE` was observed at both 1 and 0.

`CMD_ID_BATTERY_STATUS` -- the only source of a percentage -- **never answers
on this dongle**, with the link up or down, so its firmware appears not to
implement it. The parse below is kept because the command is in the vendor's
own table and other variants may support it, but it has never been seen
replying; treat any percentage it produces as unverified until it is
cross-checked against XPanel.

So this driver reports cell voltage as the level and no percentage. Converting
volts to a percentage needs Finalmouse's discharge curve, which is not in the
XPanel bundle anywhere this search could find it, and the Pulsar entry in
docs/protocols.md is the standing reminder of what guessing that curve costs.
"""

from __future__ import annotations

from dataclasses import replace

from .. import hidio
from .base import OFFLINE, DeviceInfo, Reading, matches, register

VENDOR_FINALMOUSE = 0x361D

# Every id Finalmouse ships in its own udev rules. Matching the whole family
# rather than just the one dongle in hand is safe here because the vendor
# collection is also required below -- a Finalmouse device without one is not
# something this driver will talk to.
PRODUCT_IDS = frozenset(
    {0x0100, 0x0101, 0x0102, 0x0103, 0x0104, 0x0111, 0x0200, 0x0201, 0x0202, 0x0203}
)

VENDOR_USAGE_PAGE = 0xFF00

REPORT_OUT = 0x04
REPORT_IN = 0x05
PAYLOAD_LEN = 63

# The high bit on the command byte marks a request; replies echo it clear.
REQUEST_FLAG = 0x80

CMD_HELLO = 0x00
CMD_VBAT = 0x05
CMD_RSSI = 0x0D
CMD_LINK_STATE = 0x24
CMD_BATTERY_CHARGING = 0x25
CMD_BATTERY_STATUS = 0x26

REPLY_TIMEOUT_MS = 400
REPLY_ATTEMPTS = 6
DRAIN_LIMIT = 32

# The USB strings say "Finalmouse UltralightX dongle" for every variant, so the
# model comes from the product id where it is known.
MODEL_NAMES = {0x0100: "Finalmouse UltralightX"}


def build_frame(command: int, args: bytes = b"") -> bytes:
    """One request, padded to the full 64 bytes including the report id."""
    if len(args) > PAYLOAD_LEN - 3:
        raise ValueError("args too long for this report size")
    payload = bytearray(PAYLOAD_LEN)
    payload[0] = 2 + len(args)
    payload[1] = REQUEST_FLAG | command
    payload[2] = len(args)
    payload[3 : 3 + len(args)] = args
    return bytes([REPORT_OUT]) + bytes(payload)


def matches_reply(response: bytes, command: int) -> bool:
    """True when this report answers the command we sent.

    The RSSI broadcast shares the channel, so identity is the echoed command
    byte, not arrival order.
    """
    if len(response) < 4:
        return False
    return response[0] == REPORT_IN and response[2] == command


def reply_body(response: bytes) -> bytes:
    """The argument bytes of a reply, trimmed to the length it declares."""
    if len(response) < 4:
        return b""
    return response[4 : 4 + response[3]]


def parse_battery_status(body: bytes) -> Reading:
    """CMD_ID_BATTERY_STATUS: state of charge, then millivolts little-endian.

    XPanel parses this reply as `{soc: data[0], voltage_mv: u16le(data, 1)}`.
    """
    if len(body) < 3:
        return OFFLINE
    percent = body[0]
    millivolts = body[1] | (body[2] << 8)
    if not 0 <= percent <= 100:
        # The device answered, so it is awake -- it just did not give a level
        # this driver is willing to report as one.
        return Reading(online=True, millivolts=millivolts or None)
    return Reading(online=True, percent=percent, millivolts=millivolts or None)


def parse_voltage(body: bytes) -> int | None:
    """CMD_ID_VBAT: millivolts, little-endian 16-bit."""
    if len(body) < 2:
        return None
    return body[0] | (body[1] << 8)


def format_volts(millivolts: int) -> str:
    """Cell voltage as the level, for a device that reports no percentage.

    Surfaced as a bucket rather than converted into a number: turning volts
    into a percentage needs a discharge curve, and this app does not have
    Finalmouse's.
    """
    return f"{millivolts / 1000:.2f} V"


def is_linked(body: bytes) -> bool:
    """CMD_ID_LINK_STATE. Zero is what puts XPanel into 'waiting for the
    mouse; move it to wake it up'."""
    return bool(body) and body[0] != 0


class FinalmouseDriver:
    name = "finalmouse"
    vendor_ids = frozenset({VENDOR_FINALMOUSE})

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """The vendor collection of each Finalmouse device, once."""
        best: dict[tuple, DeviceInfo] = {}
        for info in infos:
            if not matches(
                info,
                vendor_id=VENDOR_FINALMOUSE,
                product_ids=PRODUCT_IDS,
                usage_page=VENDOR_USAGE_PAGE,
            ):
                continue
            ident = (
                info["vendor_id"],
                info["product_id"],
                info.get("serial_number") or "",
            )
            best.setdefault(ident, info)
        return list(best.values())

    def label(self, info: DeviceInfo) -> str | None:
        return MODEL_NAMES.get(info.get("product_id"))

    @staticmethod
    def _drain(dev) -> None:
        """Discard the RSSI broadcasts already queued.

        Without this the first read of a transaction is whatever the dongle
        volunteered a moment ago, and every exchange starts one report behind.
        """
        for _ in range(DRAIN_LIMIT):
            if not dev.read(64, 2):
                return

    def _transact(self, dev, command: int, args: bytes = b"") -> bytes | None:
        self._drain(dev)
        dev.write(build_frame(command, args))
        for _ in range(REPLY_ATTEMPTS):
            response = bytes(dev.read(64, REPLY_TIMEOUT_MS))
            if not response:
                return None
            if matches_reply(response, command):
                return reply_body(response)
            # Anything else is the RSSI stream or another command's reply.
        return None

    def read(self, info: DeviceInfo) -> Reading:
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)

            # Link state first, and it is the cheap question: the dongle
            # answers it whether or not the mouse is awake, while an asleep
            # mouse makes CMD_ID_BATTERY_STATUS cost a full read timeout. A
            # driver that asks in the other order pays that on every poll for
            # a mouse sitting in a drawer.
            link = self._transact(dev, CMD_LINK_STATE)
            if link is not None and not is_linked(link):
                return OFFLINE

            charging_body = self._transact(dev, CMD_BATTERY_CHARGING)
            charging = bool(charging_body[0]) if charging_body else None

            # Preferred when the firmware implements it: one reply carrying
            # both the state of charge and the voltage.
            status = self._transact(dev, CMD_BATTERY_STATUS)
            if status:
                reading = parse_battery_status(status)
                if reading.online:
                    return replace(
                        reading, charging=charging, connection="2.4 GHz"
                    )

            # This dongle never answers it, linked or not, so the level here
            # is cell voltage and nothing else. Presence is still established:
            # CMD_ID_LINK_STATE is an exchange with the dongle that reports
            # whether the *mouse* is on the air, which is the thing enumeration
            # alone could never tell us.
            millivolts = parse_voltage(self._transact(dev, CMD_VBAT) or b"")
            if not millivolts:
                return OFFLINE
            return Reading(
                online=True,
                millivolts=millivolts,
                bucket=format_volts(millivolts),
                charging=charging,
                connection="2.4 GHz",
            )


register(FinalmouseDriver())
