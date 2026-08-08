"""Logitech HID++ 2.0 battery.

Two-step protocol: feature IDs are not fixed indices, so you first ask the root
feature (index 0) to resolve e.g. 0x1004 into this device's feature index, then
call that index.

Feature preference:
  0x1004 UNIFIED_BATTERY  - reports a real state-of-charge percentage
  0x1000 BATTERY_STATUS   - older mice; often reports only coarse buckets, in
                            which case no percentage is invented

Requests go out as output reports and the reply arrives as an input report.
Devices also emit unsolicited notifications on the same channel, so replies are
matched on (device index, feature index, function|software id) rather than just
taking the next report that arrives.

STATUS: verified on a G Pro X Superlight 2 via a Lightspeed receiver
(046d:c54d), cross-checked against Logitech Onboard Memory Manager.

Observed on that device: it answers on receiver slot 0x01, not the
direct-connect index 0xFF, and resolves feature 0x1004 to index 6 -- so it
reports a true state of charge rather than a bucket. Sweeping the device
indices matters: querying only 0xFF returns nothing at all.
"""

from __future__ import annotations

from .. import hidio
from .base import (
    OFFLINE,
    VENDOR_LOGITECH,
    DeviceInfo,
    Reading,
    matches,
    register,
)

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
LEN_SHORT = 7
LEN_LONG = 20

# Marks our own requests so notifications can be told apart from replies.
SOFTWARE_ID = 0x0A

ERROR_HIDPP10 = 0x8F
ERROR_HIDPP20 = 0xFF

# HID++ 1.0 resource error: the receiver has no such device / it is powered off.
ERR_RESOURCE = 0x09

ROOT_FEATURE_INDEX = 0x00
FEATURE_UNIFIED_BATTERY = 0x1004
FEATURE_BATTERY_STATUS = 0x1000

# 0xFF addresses a directly connected (wired or dedicated-dongle) device;
# 0x01-0x06 are receiver slots.
DEVICE_INDICES = (0xFF, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06)

CHARGING_STATES = {1, 2, 4}  # recharging, almost full, slow recharge


def build_request(
    device_index: int,
    feature_index: int,
    function: int,
    params: bytes = b"",
    long: bool = True,
) -> bytes:
    """[report id, device index, feature index, (func<<4)|sw id, params...]"""
    length = LEN_LONG if long else LEN_SHORT
    report = bytearray(length)
    report[0] = REPORT_LONG if long else REPORT_SHORT
    report[1] = device_index
    report[2] = feature_index
    report[3] = ((function & 0x0F) << 4) | SOFTWARE_ID
    if len(params) > length - 4:
        raise ValueError("params too long for this report size")
    report[4 : 4 + len(params)] = params
    return bytes(report)


def is_error(response: bytes) -> int | None:
    """Return the HID++ error code if this report is an error reply."""
    if len(response) < 5:
        return None
    if response[0] == ERROR_HIDPP10:
        return response[3]
    if response[0] == ERROR_HIDPP20:
        return response[4]
    return None


def matches_request(response: bytes, device_index: int, feature_index: int, function: int) -> bool:
    if len(response) < 4:
        return False
    if response[0] not in (REPORT_SHORT, REPORT_LONG):
        return False
    return (
        response[1] == device_index
        and response[2] == feature_index
        and response[3] == (((function & 0x0F) << 4) | SOFTWARE_ID)
    )


def parse_unified_battery(params: bytes) -> Reading:
    """0x1004 GetStatus: [state_of_charge, level, charging_status, ext_power]."""
    if len(params) < 3:
        return OFFLINE
    percent = params[0]
    charging = params[2] in CHARGING_STATES
    if not 0 <= percent <= 100:
        return OFFLINE
    return Reading(online=True, percent=percent, charging=charging)


def parse_battery_status(params: bytes) -> Reading:
    """0x1000 GetBatteryLevelStatus: [discharge_level, next_level, status].

    `discharge_level` is a percentage on devices that support it and 0 on those
    that only report coarse buckets. A bucket is reported as a bucket -- never
    converted into a made-up number.
    """
    if len(params) < 3:
        return OFFLINE
    level = params[0]
    charging = params[2] in CHARGING_STATES
    if 1 <= level <= 100:
        return Reading(online=True, percent=level, charging=charging)
    return Reading(online=True, bucket="unknown", charging=charging)


class LogitechDriver:
    name = "logitech"
    vendor_ids = frozenset({VENDOR_LOGITECH})

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        """Prefer the long-report vendor collection (0xff00 / usage 0x0002)."""
        best: dict[tuple, tuple[int, DeviceInfo]] = {}
        for info in infos:
            if not matches(info, vendor_id=VENDOR_LOGITECH):
                continue
            usage_page = info.get("usage_page") or 0
            usage = info.get("usage") or 0
            if not 0xFF00 <= usage_page <= 0xFFFF:
                continue
            score = 100 if usage == 0x0002 else 50 if usage == 0x0001 else 10
            ident = (info["vendor_id"], info["product_id"], info.get("serial_number") or "")
            current = best.get(ident)
            if current is None or score > current[0]:
                best[ident] = (score, info)
        return [info for _, info in best.values()]

    def _transact(
        self, dev, device_index: int, feature_index: int, function: int, params: bytes = b""
    ) -> bytes | None:
        """Send a request and return the matching reply's parameter bytes."""
        dev.write(build_request(device_index, feature_index, function, params))
        # Drain up to a few reports: notifications can arrive interleaved.
        for _ in range(8):
            response = bytes(dev.read(LEN_LONG, 400))
            if not response:
                return None
            code = is_error(response)
            if code is not None:
                return None
            if matches_request(response, device_index, feature_index, function):
                return response[4:]
        return None

    def _feature_index(self, dev, device_index: int, feature_id: int) -> int | None:
        params = bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00])
        reply = self._transact(dev, device_index, ROOT_FEATURE_INDEX, 0x00, params)
        if not reply or not reply[0]:
            return None  # index 0 means "feature not supported"
        return reply[0]

    def _read_device(self, dev, device_index: int) -> Reading | None:
        index = self._feature_index(dev, device_index, FEATURE_UNIFIED_BATTERY)
        if index is not None:
            reply = self._transact(dev, device_index, index, 0x01)
            if reply:
                return parse_unified_battery(reply)

        index = self._feature_index(dev, device_index, FEATURE_BATTERY_STATUS)
        if index is not None:
            reply = self._transact(dev, device_index, index, 0x00)
            if reply:
                return parse_battery_status(reply)
        return None

    def read(self, info: DeviceInfo) -> Reading:
        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)
            for device_index in DEVICE_INDICES:
                try:
                    reading = self._read_device(dev, device_index)
                except Exception:
                    continue
                if reading is not None:
                    return reading
        return OFFLINE


register(LogitechDriver())
