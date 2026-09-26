"""Pulsar battery protocol.

17 bytes on the wire: report ID 8, then a 16-byte frame. The last byte is a
checksum chosen so the whole wire frame sums to K; Pulsar uses K = 0x55, IPI's
Stay Fly firmware uses K = 0x4D over the same shape, hence the parameter.

    [0]  report id (8)
    [1]  command            (4 = BatteryLevel)
    [2:5] reserved
    [5]  payload length
    [6:] payload
    [16] checksum

Response, same framing, with the command echoed at [1]:

    [6]    percent
    [7]    1 = charging
    [8:10] cell millivolts, big endian

Confirmed against Pulsar's own WebHID configurator (bbb.pulsar.gg/cMouse), whose
`os()` builds the frame and whose input handler reads exactly these offsets. Its
`s[15] = checksum - gt` with `gt = 8` compensates for WebHID excluding the report
ID from the data array -- so on the wire, all 17 bytes sum to 0x55.

Sent as an output report and read back as an input report. An earlier version
used feature reports; that was a guess and it was wrong -- the vendor app calls
`sendReport`, and the pyusb reference reads an interrupt endpoint.

STATUS: verified on hardware against a Pulsar X2 CrazyLight (3710:5406), matching
the vendor configurator. The older 0x3554 platform shares the frame layout but
has not been tested.
"""

from __future__ import annotations

import time
from dataclasses import replace

from .. import hidio
from .base import (
    OFFLINE,
    VENDOR_PULSAR,
    DeviceInfo,
    Reading,
    matches,
    register,
)

REPORT_ID = 0x08
FRAME_LEN = 17
CHECKSUM_PULSAR = 0x55
CHECKSUM_IPI = 0x4D

CMD_DEVICE_ONLINE = 0x03  # rt.DeviceOnLine -- online flag plus device address
CMD_POWER = 0x04  # rt.BatteryLevel in the vendor bundle

# Newer "Pulsar 8K Dongle" platform, e.g. the X2 CrazyLight.
VENDOR_PULSAR_8K = 0x3710
# Hitscan uses the same receiver platform: identical collection layout, frame
# format and checksum. Only the battery scaling differs -- see below.
VENDOR_HITSCAN = 0x3770

VENDOR_IDS = frozenset({VENDOR_PULSAR, VENDOR_PULSAR_8K, VENDOR_HITSCAN})

# The whole platform derives its displayed percentage from cell voltage, not
# from the level byte the receiver reports. Confirmed against both vendors'
# own software, and in both cases the level byte disagreed:
#
#   Pulsar X2N   level 95%, 4122 mV -> their app says 100%, curve says 100%
#   Hitscan      level 70%, 4176 mV -> their app says 100%, curve says 100%
#
# So the level byte is the unreliable one. This was initially restricted to
# Pulsar out of caution about applying one vendor's calibration to another's
# cells; the Hitscan reading settled it the other way.
VOLTAGE_CURVE_VENDORS = VENDOR_IDS

# Identity namespace per vendor, so two brands cannot collide on one address.
IDENTITY_PREFIXES = {
    VENDOR_PULSAR: "pulsar",
    VENDOR_PULSAR_8K: "pulsar",
    VENDOR_HITSCAN: "hitscan",
}

# The dongle exposes several vendor collections; only this one carries the
# command channel (report 0x08, 16-byte input and output). The others are
# status/telemetry and never answer a command.
COMMAND_USAGE_PAGE = 0xFF02

RESPONSE_TIMEOUT = 1.0
READ_TIMEOUT_MS = 250

# 0xf507 wired / 0xf508 2.4GHz dongle are the documented Pulsar ids; the
# lineup has grown, so any known-vendor device with a vendor collection is
# accepted.


# Pulsar's configurator does not display the level the mouse reports. It derives
# a percentage from the cell voltage using this table (millivolts), taken from
# the bundle's `w` array, and treats anything above the top entry as full.
#
# The two disagree in normal use: at 4122 mV the mouse says 95% while the table
# says 100%. Matching the vendor keeps this app consistent with the software the
# user compares it against. The raw figures are both preserved on the Reading --
# `millivolts` is the measurement, and the device's own level is what the driver
# falls back to when no voltage is reported.
VOLTAGE_TABLE = (
    3050, 3420, 3480, 3540, 3600, 3660, 3720, 3760, 3800, 3840,
    3880, 3920, 3940, 3960, 3980, 4000, 4020, 4040, 4060, 4080, 4110,
)


def percent_from_voltage(millivolts: int, charging: bool = False) -> int:
    """Vendor's voltage -> percent curve.

    Mirrors their `T(voltage, charging)`, with one deliberate difference: their
    version returns 0 when the voltage exactly equals the top table entry,
    because the lookup finds no bucket and falls through with s = 0. That is a
    bug, and reproducing it would report a full battery as empty.
    """
    if millivolts >= VOLTAGE_TABLE[-1]:
        return 99 if charging else 100

    bucket = None
    for index, threshold in enumerate(VOLTAGE_TABLE):
        if millivolts < threshold:
            bucket = index
            break
    if bucket is None:
        return 100
    if bucket == 0:
        return 0

    step = (VOLTAGE_TABLE[bucket] - VOLTAGE_TABLE[bucket - 1]) / 5
    value = (millivolts - VOLTAGE_TABLE[bucket - 1]) / step + 5 * (bucket - 1)

    # The vendor bumps the result by one when it lands exactly on 0 or 15. That
    # makes their curve non-monotonic -- 3050 mV reports 1% while 3060 mV
    # reports 0% -- so it is left out. The difference is a single point at two
    # exact table boundaries.
    return max(0, min(100, round(value)))


def checksum(body: bytes, base: int = CHECKSUM_PULSAR) -> int:
    """Trailing checksum byte: (base - sum(body)) & 0xff."""
    return (base - sum(body)) & 0xFF


def build_frame(command: int, args: bytes = b"", base: int = CHECKSUM_PULSAR) -> bytes:
    """17-byte wire frame; see the module docstring for the layout.

    The report ID byte is part of the checksummed body, so the finished frame
    sums to `base`.
    """
    frame = bytearray(FRAME_LEN)
    frame[0] = REPORT_ID
    frame[1] = command
    if len(args) > FRAME_LEN - 7:
        raise ValueError("args too long for a 17-byte frame")
    frame[5] = len(args)
    frame[6 : 6 + len(args)] = args
    frame[FRAME_LEN - 1] = checksum(bytes(frame[: FRAME_LEN - 1]), base)
    return bytes(frame)


def parse_device_online(response: bytes) -> tuple[bool, str | None]:
    """Decode a DeviceOnLine (0x03) reply -> (online, device address).

    The vendor reads `online = xt[5]` and stores the address reversed as
    `addr[2]=xt[6], addr[1]=xt[7], addr[0]=xt[8]`; our indices are one higher
    because hidapi keeps the report ID at [0].
    """
    if len(response) < 10:
        return False, None
    online = bool(response[6])
    address = bytes((response[9], response[8], response[7]))
    if not any(address):
        return online, None
    return online, address.hex()


def device_identity(address: str, vendor_id: int = VENDOR_PULSAR_8K) -> str:
    prefix = IDENTITY_PREFIXES.get(vendor_id, "compxrf")
    return f"{prefix}:{address}"


def parse_power(response: bytes, use_voltage_curve: bool = True) -> Reading:
    """Decode a POWER (0x04) response.

    Layout: [0]=report id, [1]=command echo, [6]=percent,
    [7]=external power connected, [8:10]=cell millivolts (big endian).
    """
    if len(response) < 10:
        return OFFLINE
    percent = response[6]
    charging = bool(response[7])
    millivolts = int.from_bytes(response[8:10], "big")

    # A mouse that is off answers with a zeroed frame; treat an implausible
    # percentage with no voltage as "not there" rather than reporting 0%.
    if percent == 0 and millivolts == 0:
        return OFFLINE
    if not 0 <= percent <= 100:
        return OFFLINE

    # Prefer the vendor's voltage curve so the number matches their software;
    # fall back to the level the mouse reports if no voltage came back, or if
    # this vendor has no curve of its own.
    if use_voltage_curve and millivolts:
        percent = percent_from_voltage(millivolts, charging)

    return Reading(
        online=True,
        percent=percent,
        charging=charging,
        millivolts=millivolts or None,
    )


def _collection_score(info: DeviceInfo) -> int:
    """Higher is a better candidate for the command channel."""
    usage_page = info.get("usage_page") or 0
    if usage_page == COMMAND_USAGE_PAGE:
        return 100
    # Older platforms are untested; fall back to the lowest vendor page.
    return 50 - min(usage_page - 0xFF00, 49)


class PulsarDriver:
    name = "pulsar"
    vendor_ids = VENDOR_IDS

    def __init__(self) -> None:
        # Last known device address per dongle. A dongle that answers the
        # battery query but not the identity query (or whose mouse just went to
        # sleep) still lands under the right entry instead of reverting to a
        # dongle-keyed one and creating a duplicate.
        self._addresses: dict[str, str] = {}

    @staticmethod
    def _dongle_key(info: DeviceInfo) -> str:
        return (
            f"{info.get('vendor_id'):04x}:{info.get('product_id'):04x}:"
            f"{info.get('serial_number') or ''}"
        )

    def _exchange(self, dev, command: int) -> bytes | None:
        """Send a command and return the reply that echoes it."""
        dev.write(build_frame(command))
        deadline = time.time() + RESPONSE_TIMEOUT
        while time.time() < deadline:
            response = bytes(dev.read(FRAME_LEN, READ_TIMEOUT_MS))
            if len(response) >= 10 and response[1] == command:
                return response
        return None

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Pick the command channel, one per physical device.

        The 8K dongle exposes five vendor collections and only 0xff02 answers,
        so matching "any vendor page" would usually select a silent one.
        """
        chosen: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if (info.get("vendor_id") or 0) not in VENDOR_IDS:
                continue
            usage_page = info.get("usage_page") or 0
            if not 0xFF00 <= usage_page <= 0xFFFF:
                continue
            ident = (
                info["vendor_id"],
                info["product_id"],
                info.get("serial_number") or "",
            )
            score = _collection_score(info)
            current = chosen.get(ident)
            if current is None or score > current[0]:
                chosen[ident] = (score, info)
        return [info for _, info in chosen.values()]

    def read(self, info: DeviceInfo) -> Reading:
        dongle = self._dongle_key(info)
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)

            # Ask which mouse is paired before asking about its battery: one
            # dongle can be re-paired to different mice, so the dongle's own
            # serial does not identify the mouse.
            identity_reply = self._exchange(dev, CMD_DEVICE_ONLINE)
            if identity_reply is not None:
                _, address = parse_device_online(identity_reply)
                if address:
                    self._addresses[dongle] = address

            power_reply = self._exchange(dev, CMD_POWER)

        address = self._addresses.get(dongle)
        vendor_id = info.get("vendor_id") or 0
        use_curve = vendor_id in VOLTAGE_CURVE_VENDORS
        reading = (
            OFFLINE
            if power_reply is None
            else parse_power(power_reply, use_curve)
        )

        # Tag offline readings too. Otherwise a sleeping mouse falls back to the
        # dongle key and appears as a second, phantom entry alongside itself.
        if address:
            return replace(
                reading, device_id=device_identity(address, vendor_id)
            )
        return reading


register(PulsarDriver())
