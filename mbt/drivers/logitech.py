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

The software id is rotated per request, and that is load-bearing rather than
decorative. Two consecutive root queries -- "which index is 0x1004?" then
"which index is 0x1000?" -- are byte-identical in every field the matcher can
see, and the root reply does not echo which feature was asked about. So a
reply that arrived too late to be read as 0x1004's answer would satisfy
0x1000's request instead, resolving 0x1000 to 0x1004's index. The driver then
called function 0 on it, which on 0x1004 is getCapabilities, and parsed
`0f 0f 01 00` -- supported levels and flags -- as a battery reading. That is a
flawless-looking "15%, charging" on a mouse sitting at 75%, and once the route
cache kept it, it stuck until the process restarted. Draining the queue before
each request does not close this: the offending reply is still in flight at
drain time and only lands afterwards.

STATUS: verified on a G Pro X Superlight 2 via a Lightspeed receiver
(046d:c54d), cross-checked against Logitech Onboard Memory Manager.

Observed on that device: it answers on receiver slot 0x01, not the
direct-connect index 0xFF, and resolves feature 0x1004 to index 6 -- so it
reports a true state of charge rather than a bucket. Sweeping the device
indices matters: querying only 0xFF returns nothing at all.
"""

from __future__ import annotations

from dataclasses import replace

from .. import hidio
from .base import (
    OFFLINE,
    VENDOR_LOGITECH,
    DeviceInfo,
    Reading,
    device_key,
    matches,
    register,
)

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
LEN_SHORT = 7
LEN_LONG = 20

# Marks our own requests so notifications can be told apart from replies.
# Also rotated per transaction -- see `next_software_id`.
SOFTWARE_ID = 0x0A

# 1-15 are usable; 0 is reserved for device-initiated notifications.
#
# Rotating this is what makes a late reply from the *previous* transaction
# distinguishable from this one's. Two consecutive root queries are otherwise
# byte-identical in every field the matcher can see -- same device index, same
# feature index 0, same function 0 -- and the root reply does not echo which
# feature was asked about. So a slow reply to "resolve 0x1004" satisfies the
# next request, "resolve 0x1000", handing back 0x1004's index.
SOFTWARE_IDS = tuple(range(1, 16))
_software_id_counter = 0


def next_software_id() -> int:
    """The next software id in the rotation."""
    global _software_id_counter
    value = SOFTWARE_IDS[_software_id_counter % len(SOFTWARE_IDS)]
    _software_id_counter += 1
    return value

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

UNIFIED = "unified"
LEGACY = "legacy"

# 0x0003 DEVICE_INFORMATION. getDeviceInfo returns a per-unit id that is the
# same whether the mouse is on its receiver or plugged in directly, so it can
# tie the two USB identities together. Without it a G Pro X 2 appears twice:
# once as the receiver (046d:c54d) and once wired (046d:c09b), with different
# product ids and different serials.
FEATURE_DEVICE_INFO = 0x0003
UNIT_ID_OFFSET = 1
UNIT_ID_LENGTH = 4

# Unit ids do not change, so they are looked up once per device per process.
_unit_cache: dict[tuple, str] = {}

# Which (device index, feature index, kind) answered, per device. Sweeping is
# expensive, so it is done once and remembered.
_route_cache: dict[tuple, tuple[int, int, str]] = {}
_route_misses: dict[tuple, int] = {}

# Consecutive failures on a cached route before re-discovering. Low enough that
# a re-paired mouse is found quickly, high enough that a sleeping one does not
# trigger a full sweep on every poll.
ROUTE_MISS_LIMIT = 3

# The two battery features use *different* chargingStatus enums, so one shared
# set was wrong for whichever feature it did not describe.
#
# 0x1000 batteryStatus: 0 discharging, 1 recharging, 2 almost full,
#   3 charged, 4 slow recharge, 5 invalid battery, 6 thermal error, 7 other.
LEGACY_CHARGING_STATES = {1, 2, 4}

# 0x1004 chargingStatus: 0 discharging, 1 charging, 2 charging slow,
#   3 charge complete, 4 charge error. Note 4 means the opposite here of what
#   it means above, which is exactly why these are now separate.
UNIFIED_CHARGING_STATES = {1, 2, 3}


def build_request(
    device_index: int,
    feature_index: int,
    function: int,
    params: bytes = b"",
    long: bool = True,
    software_id: int = SOFTWARE_ID,
) -> bytes:
    """[report id, device index, feature index, (func<<4)|sw id, params...]"""
    length = LEN_LONG if long else LEN_SHORT
    report = bytearray(length)
    report[0] = REPORT_LONG if long else REPORT_SHORT
    report[1] = device_index
    report[2] = feature_index
    report[3] = ((function & 0x0F) << 4) | (software_id & 0x0F)
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


def addresses_request(
    response: bytes,
    device_index: int,
    feature_index: int,
    function: int,
    software_id: int = SOFTWARE_ID,
) -> bool:
    """True when this report answers the exact request we sent.

    Shared by replies and error reports: a HID++ error echoes the same device
    index, feature index and function/software-id byte as the request it is
    rejecting, just under a different report id.
    """
    if len(response) < 4:
        return False
    return (
        response[1] == device_index
        and response[2] == feature_index
        and response[3] == (((function & 0x0F) << 4) | (software_id & 0x0F))
    )


def parse_unified_battery(params: bytes) -> Reading:
    """0x1004 GetStatus: [state_of_charge, level, charging_status, ext_power]."""
    if len(params) < 3:
        return OFFLINE
    percent = params[0]
    charging = params[2] in UNIFIED_CHARGING_STATES
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
    charging = params[2] in LEGACY_CHARGING_STATES
    if 1 <= level <= 100:
        return Reading(online=True, percent=level, charging=charging)
    return Reading(online=True, bucket="unknown", charging=charging)


class LogitechDriver:
    name = "logitech"

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

    @staticmethod
    def _drain(dev) -> None:
        """Discard anything already queued before starting a transaction.

        Replies left over from a timed-out exchange stay in the OS queue. The
        next transaction then reads them first, and if one happens to satisfy
        the new match it is accepted as this reply -- which is how a feature
        index gets resolved to the wrong number and every later battery read
        returns a plausible but wrong percentage.
        """
        # NOT a timeout of 0: cython-hidapi treats 0 as "no timeout given" and
        # falls through to a blocking hid_read(), which never returns when the
        # queue is empty. 1 ms is effectively non-blocking and always returns.
        for _ in range(16):
            if not dev.read(LEN_LONG, 1):
                return

    def _transact(
        self, dev, device_index: int, feature_index: int, function: int, params: bytes = b""
    ) -> bytes | None:
        """Send a request and return the matching reply's parameter bytes."""
        self._drain(dev)
        # A fresh software id per request, so a reply that arrives late cannot
        # be mistaken for the answer to whatever we asked next.
        software_id = next_software_id()
        dev.write(
            build_request(
                device_index, feature_index, function, params, software_id=software_id
            )
        )
        # Read a few reports: notifications and stale replies arrive interleaved.
        for _ in range(8):
            response = bytes(dev.read(LEN_LONG, 400))
            if not response:
                return None
            if not addresses_request(
                response, device_index, feature_index, function, software_id
            ):
                # Someone else's report -- a notification, or a reply to a
                # request that timed out earlier. Previously an unrelated error
                # report aborted this transaction, which then sent the caller
                # down the fallback path with a stale reply waiting for it.
                continue
            if is_error(response) is not None:
                return None
            if response[0] in (REPORT_SHORT, REPORT_LONG):
                return response[4:]
        return None

    def _feature_index(self, dev, device_index: int, feature_id: int) -> int | None:
        params = bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00])
        reply = self._transact(dev, device_index, ROOT_FEATURE_INDEX, 0x00, params)
        if not reply or not reply[0]:
            return None  # index 0 means "feature not supported"
        return reply[0]

    def _call_feature(
        self, dev, device_index: int, feature_index: int, kind: str
    ) -> Reading | None:
        """Invoke an already-resolved battery feature."""
        if kind == UNIFIED:
            reply = self._transact(dev, device_index, feature_index, 0x01)
            return parse_unified_battery(reply) if reply else None
        reply = self._transact(dev, device_index, feature_index, 0x00)
        return parse_battery_status(reply) if reply else None

    def _discover_route(self, dev, device_index: int):
        """Find a working battery feature on this slot -> (index, kind, reading)."""
        seen: dict[int, int] = {}
        for feature_id, kind, function in (
            (FEATURE_UNIFIED_BATTERY, UNIFIED, 0x01),
            (FEATURE_BATTERY_STATUS, LEGACY, 0x00),
        ):
            index = self._feature_index(dev, device_index, feature_id)
            if index is None:
                continue
            # Every feature has its own index, so two feature ids resolving to
            # the same one means a reply was matched to the wrong request. The
            # damage that does is quiet and specific: 0x1000 inheriting
            # 0x1004's index makes the driver call function 0 on it, and
            # 0x1004's getCapabilities reply (0f 0f 01 ...) parses as a
            # perfectly plausible "15%, charging".
            if index in seen:
                continue
            seen[index] = feature_id
            reply = self._transact(dev, device_index, index, function)
            if not reply:
                continue
            reading = (
                parse_unified_battery(reply)
                if kind == UNIFIED
                else parse_battery_status(reply)
            )
            # Only remember a route that produced a usable answer. Caching an
            # index resolved from a desynced reply would make one bad exchange
            # stick, reporting a wrong percentage indefinitely.
            if not reading.online:
                continue
            return index, kind, reading
        return None

    def _unit_id(self, dev, device_index: int) -> str | None:
        """Per-unit id from DEVICE_INFORMATION, stable across transports."""
        index = self._feature_index(dev, device_index, FEATURE_DEVICE_INFO)
        if index is None:
            return None
        reply = self._transact(dev, device_index, index, 0x00)
        if not reply or len(reply) < UNIT_ID_OFFSET + UNIT_ID_LENGTH:
            return None
        unit = bytes(reply[UNIT_ID_OFFSET : UNIT_ID_OFFSET + UNIT_ID_LENGTH])
        if not any(unit):
            return None
        return unit.hex()

    def _identity_for(self, dev, ident: tuple, device_index: int) -> str | None:
        cached = _unit_cache.get(ident)
        if cached is not None:
            return cached
        try:
            unit = self._unit_id(dev, device_index)
        except Exception:
            unit = None
        if unit:
            _unit_cache[ident] = unit
        return unit

    def read(self, info: DeviceInfo) -> Reading:
        ident = (
            info["vendor_id"],
            info["product_id"],
            info.get("serial_number") or "",
        )

        with hidio.open_path(info["path"]) as dev:
            dev.set_nonblocking(False)

            # Fast path. Sweeping all seven receiver slots costs a 400 ms
            # timeout each time one does not answer -- over five seconds for a
            # sleeping mouse, on every single poll. Remembering the slot and
            # feature that worked turns that into one call.
            cached = _route_cache.get(ident)
            if cached is not None:
                device_index, feature_index, kind = cached
                try:
                    reading = self._call_feature(dev, device_index, feature_index, kind)
                except Exception:
                    reading = None
                if reading is not None:
                    _route_misses[ident] = 0
                    return self._tag(dev, reading, ident, device_index)

                # Do not re-sweep on the first miss: a sleeping mouse would pay
                # the full cost every poll. Only give up on the cached route
                # after several failures, which also covers a re-pair.
                misses = _route_misses.get(ident, 0) + 1
                _route_misses[ident] = misses
                if misses < ROUTE_MISS_LIMIT:
                    return OFFLINE
                _route_cache.pop(ident, None)
                _route_misses[ident] = 0

            for device_index in DEVICE_INDICES:
                try:
                    found = self._discover_route(dev, device_index)
                except Exception:
                    continue
                if found is None:
                    continue
                feature_index, kind, reading = found
                _route_cache[ident] = (device_index, feature_index, kind)
                _route_misses[ident] = 0
                return self._tag(dev, reading, ident, device_index)
        return OFFLINE

    def _tag(self, dev, reading: Reading, ident: tuple, device_index: int) -> Reading:
        """Attach the unit id so wired and wireless share one entry."""
        unit = self._identity_for(dev, ident, device_index)
        if not unit:
            return reading
        return replace(reading, device_id=f"logitech:{unit}")


def legacy_aliases() -> dict[str, str]:
    """Serial-keyed identity -> the unit id now used instead, for every device
    that has answered a DEVICE_INFORMATION query this session.

    Unlike ipi.py's static table, this cannot be a fixed mapping: the unit id
    is only learned from a live exchange (`_tag`, called from `read`), so
    nothing is known about a device before it has answered at least once. The
    app calls this after every poll rather than only at startup for exactly
    that reason -- a mouse can still be running under its old serial-based key
    the first time this is consulted.

    Without it, a mouse's history and user-assigned name stay stranded under
    the old key forever: the wired/wireless merge that unit ids exist to
    provide only benefits readings from this point forward, not what was
    already on disk.
    """
    aliases = {}
    for (vid, pid, serial), unit in _unit_cache.items():
        if vid != VENDOR_LOGITECH:
            continue
        old_key = device_key(
            {"vendor_id": vid, "product_id": pid, "serial_number": serial}
        )
        aliases[old_key] = f"logitech:{unit}"
    return aliases


register(LogitechDriver())
