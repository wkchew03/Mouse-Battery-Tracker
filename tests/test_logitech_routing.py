"""Logitech transaction hygiene and route caching.

Both behaviours here fixed real faults found on hardware:

- Sweeping all seven receiver slots cost ~6 s per poll, which made "Refresh
  now" look broken because nothing happened for six seconds.
- Stale replies left in the queue could satisfy the next transaction's match,
  resolving a feature index to the wrong number and reporting a plausible but
  wrong percentage (85% one moment, 15% the next).
"""

import pytest

from mbt.drivers import logitech
from mbt.drivers.base import Reading


class Verbatim(bytes):
    """A reply whose software id must NOT be rewritten to match the request.

    Everything else the fake device serves is echoed back with the request's
    own id, as hardware does. This one belongs to an *earlier* request that
    timed out and is only now arriving -- keeping its original id is the whole
    point of the fixture.
    """


class FakeDevice:
    """Minimal stand-in that records reads and their timeouts."""

    def __init__(self, queued=None, replies=None):
        # Already sitting in the OS queue before we do anything -- what a
        # previous timed-out transaction leaves behind.
        self.queued = list(queued or [])
        # Served one per write, because a reply only exists once asked for.
        self.replies = list(replies or [])
        self.pending = []
        self.reads = []
        self.writes = []
        self.blocking = True

    def set_nonblocking(self, value):
        self.blocking = not value

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if self.replies:
            raw = self.replies.pop(0)
            if isinstance(raw, Verbatim):
                self.pending.append(bytes(raw))
            else:
                reply = bytearray(raw)
                # Echo back the software id this request actually used. The
                # driver rotates it per transaction and real hardware returns
                # byte 3 verbatim, so a fixture answering with a fixed id
                # would only ever exercise the case where they line up.
                if len(reply) > 3 and len(data) > 3:
                    reply[3] = (reply[3] & 0xF0) | (data[3] & 0x0F)
                self.pending.append(bytes(reply))
        return len(data)

    def read(self, length, timeout=0):
        self.reads.append(timeout)
        if timeout == 0 and self.blocking:
            raise AssertionError(
                "read(timeout=0) on a blocking handle waits forever in hidapi"
            )
        if self.queued:
            return list(self.queued.pop(0))
        if self.pending:
            return list(self.pending.pop(0))
        return []


def _reply(device_index, feature_index, function, params):
    frame = bytearray(logitech.LEN_LONG)
    frame[0] = logitech.REPORT_LONG
    frame[1] = device_index
    frame[2] = feature_index
    frame[3] = (function << 4) | logitech.SOFTWARE_ID
    frame[4 : 4 + len(params)] = params
    return bytes(frame)


@pytest.fixture(autouse=True)
def clear_caches():
    for cache in (logitech._route_cache, logitech._route_misses, logitech._unit_cache):
        cache.clear()
    yield
    for cache in (logitech._route_cache, logitech._route_misses, logitech._unit_cache):
        cache.clear()


# --------------------------------------------------------------------------
# Draining
# --------------------------------------------------------------------------


def test_drain_never_uses_a_zero_timeout():
    """A zero timeout falls through to a blocking hid_read() and hangs the poll
    thread -- which took the whole app down until it was caught."""
    dev = FakeDevice(queued=[_reply(1, 6, 1, [50, 0, 0, 0])])
    logitech.LogitechDriver._drain(dev)
    assert dev.reads
    assert all(timeout > 0 for timeout in dev.reads)


def test_drain_empties_the_queue():
    stale = [_reply(1, 6, 1, [50, 0, 0, 0]), _reply(1, 6, 1, [51, 0, 0, 0])]
    dev = FakeDevice(queued=stale)
    logitech.LogitechDriver._drain(dev)
    assert dev.queued == []


def test_transaction_discards_stale_replies_first():
    """The stale reply would otherwise be returned as this request's answer."""
    stale = _reply(1, 6, 1, [15, 0, 0, 0])
    fresh = _reply(1, 6, 1, [85, 0, 0, 0])
    dev = FakeDevice(queued=[stale], replies=[fresh])
    params = logitech.LogitechDriver()._transact(dev, 1, 6, 1)
    assert params[0] == 85


# --------------------------------------------------------------------------
# Route caching
# --------------------------------------------------------------------------


def test_offline_result_is_not_cached():
    """Caching a route that produced no usable reading would make one bad
    exchange stick and misreport indefinitely."""
    driver = logitech.LogitechDriver()
    # Root resolves to feature 6, but the battery call reports 200% -> rejected.
    dev = FakeDevice(
        replies=[
            _reply(1, 0, 0, [6, 0, 0]),
            _reply(1, 6, 1, [200, 0, 0, 0]),
            _reply(1, 0, 0, [0, 0, 0]),
        ]
    )
    assert driver._discover_route(dev, 1) is None


def test_route_is_cached_after_a_good_read():
    driver = logitech.LogitechDriver()
    dev = FakeDevice(
        replies=[_reply(1, 0, 0, [6, 0, 0]), _reply(1, 6, 1, [85, 0, 0, 0])]
    )
    found = driver._discover_route(dev, 1)
    assert found is not None
    index, kind, reading = found
    assert index == 6
    assert kind == logitech.UNIFIED
    assert reading.percent == 85


def test_cached_route_skips_feature_resolution():
    """The whole point: one call instead of resolving features again."""
    driver = logitech.LogitechDriver()
    dev = FakeDevice(replies=[_reply(1, 6, 1, [85, 0, 0, 0])])
    reading = driver._call_feature(dev, 1, 6, logitech.UNIFIED)
    assert reading.percent == 85
    assert len(dev.writes) == 1


def test_miss_limit_allows_rediscovery():
    """A re-paired mouse must eventually be found again rather than being
    stuck on a dead cached route."""
    assert logitech.ROUTE_MISS_LIMIT >= 2


# --------------------------------------------------------------------------
# Unit id -- ties the wired and wireless identities together
# --------------------------------------------------------------------------


def test_unit_id_is_read_from_device_information():
    """Without this a G Pro X 2 appears twice: once via its receiver
    (046d:c54d) and once wired (046d:c09b), with different ids and serials."""
    driver = logitech.LogitechDriver()
    dev = FakeDevice(
        replies=[
            _reply(1, 0, 0, [4, 0, 0]),  # 0x0003 resolves to feature index 4
            _reply(1, 4, 0, [1, 0xE4, 0xCD, 0x8E, 0x68]),
        ]
    )
    assert driver._unit_id(dev, 1) == "e4cd8e68"


def test_unit_id_ignores_an_all_zero_value():
    driver = logitech.LogitechDriver()
    dev = FakeDevice(
        replies=[_reply(1, 0, 0, [4, 0, 0]), _reply(1, 4, 0, [1, 0, 0, 0, 0])]
    )
    assert driver._unit_id(dev, 1) is None


def test_unit_id_is_none_when_the_feature_is_unsupported():
    driver = logitech.LogitechDriver()
    dev = FakeDevice(replies=[_reply(1, 0, 0, [0, 0, 0])])
    assert driver._unit_id(dev, 1) is None


def test_identity_is_cached_after_the_first_lookup():
    """The unit id never changes, so it must not cost a transaction per poll."""
    driver = logitech.LogitechDriver()
    ident = (0x046D, 0xC54D, "serial")
    dev = FakeDevice(
        replies=[
            _reply(1, 0, 0, [4, 0, 0]),
            _reply(1, 4, 0, [1, 0xE4, 0xCD, 0x8E, 0x68]),
        ]
    )
    assert driver._identity_for(dev, ident, 1) == "e4cd8e68"
    writes_after_first = len(dev.writes)
    assert driver._identity_for(dev, ident, 1) == "e4cd8e68"
    assert len(dev.writes) == writes_after_first


def test_tagged_reading_carries_a_namespaced_id():
    driver = logitech.LogitechDriver()
    ident = (0x046D, 0xC09B, "wired")
    logitech._unit_cache[ident] = "e4cd8e68"
    tagged = driver._tag(FakeDevice(), Reading(online=True, percent=60), ident, 1)
    assert tagged.device_id == "logitech:e4cd8e68"


def test_same_unit_id_from_either_transport_gives_one_entry():
    """The whole point: receiver and wired resolve to the same key."""
    driver = logitech.LogitechDriver()
    wireless = (0x046D, 0xC54D, "375037593432")
    wired = (0x046D, 0xC09B, "E4CD8E68")
    logitech._unit_cache[wireless] = "e4cd8e68"
    logitech._unit_cache[wired] = "e4cd8e68"

    a = driver._tag(FakeDevice(), Reading(online=True, percent=79), wireless, 1)
    b = driver._tag(FakeDevice(), Reading(online=True, percent=79), wired, 255)
    assert a.device_id == b.device_id


# --------------------------------------------------------------------------
# legacy_aliases -- migrating a mouse from its serial-keyed identity to the
# unit id, once one has actually been read
# --------------------------------------------------------------------------


def test_legacy_aliases_is_empty_before_anything_has_answered():
    """The unit id cannot be known ahead of a live read, so there is nothing
    to migrate on a cold start."""
    assert logitech.legacy_aliases() == {}


def test_legacy_aliases_maps_the_old_serial_key_to_the_unit_id():
    logitech._unit_cache[(0x046D, 0xC54D, "375037593432")] = "e4cd8e68"
    assert logitech.legacy_aliases() == {
        "046d:c54d:375037593432": "logitech:e4cd8e68"
    }


def test_legacy_aliases_covers_every_transport_a_device_was_seen_on():
    """Both the wireless and wired identities fold into the same new key."""
    logitech._unit_cache[(0x046D, 0xC54D, "375037593432")] = "e4cd8e68"
    logitech._unit_cache[(0x046D, 0xC09B, "E4CD8E68")] = "e4cd8e68"
    aliases = logitech.legacy_aliases()
    assert aliases["046d:c54d:375037593432"] == "logitech:e4cd8e68"
        # device_key() does not case-fold the serial, so it survives as reported.
    assert aliases["046d:c09b:E4CD8E68"] == "logitech:e4cd8e68"


def test_legacy_aliases_ignores_other_vendors():
    """Sanity check: the cache is process-wide, so a stray entry from another
    driver must never leak into Logitech's own aliasing."""
    logitech._unit_cache[(0x1532, 0x00C1, "razer-serial")] = "deadbeef"
    assert logitech.legacy_aliases() == {}


# --------------------------------------------------------------------------
# The stale root reply that reported 15% on a mouse at 75%
#
# Captured from a G Pro X Superlight 2 on its Lightspeed receiver, which
# reported this for weeks: the vendor's Onboard Memory Manager showed 75%
# while the tray showed "15%, charging". Both numbers came off the same
# device, from two different replies.
# --------------------------------------------------------------------------

# 0x1004 getStatus  -> 75%, discharging. What the mouse actually reports.
UNIFIED_STATUS = bytes([0x4B, 0x08, 0x00, 0x00])

# 0x1004 getCapabilities -> supported levels + flags. Nothing to do with
# charge, but parsed as a battery reply it reads as a flawless "15%, charging".
UNIFIED_CAPABILITIES = bytes([0x0F, 0x0F, 0x01, 0x00])


def test_the_capabilities_reply_is_what_15_percent_was():
    """Pin down the coincidence that made this so hard to see: the bad value
    was never noise, it was a real reply to a different question."""
    assert logitech.parse_battery_status(UNIFIED_CAPABILITIES) == Reading(
        online=True, percent=15, charging=True
    )
    assert logitech.parse_unified_battery(UNIFIED_STATUS) == Reading(
        online=True, percent=75, charging=False
    )


def _resolve_with_a_late_reply_from_the_previous_query(driver):
    """Ask about 0x1004 and get no answer in time, then ask about 0x1000 and
    have 0x1004's answer turn up instead.

    Returns what the second query resolved to. This is the exact sequence the
    receiver produced on hardware, and note *when* the stale reply arrives:
    after the drain, not before it. A reply already sitting in the queue is
    what `_drain` exists to throw away; one still in flight when the next
    request goes out is the case it cannot cover.
    """
    device = FakeDevice(replies=[[]])  # 0x1004's root query: no answer in time
    assert driver._feature_index(device, 0x01, logitech.FEATURE_UNIFIED_BATTERY) is None
    first_id = device.writes[-1][3] & 0x0F

    # Now it arrives, mid-way through the *next* transaction, still carrying
    # the software id of the request it belongs to.
    stale = bytearray(_reply(0x01, 0x00, 0x00, bytes([6, 0, 5])))
    stale[3] = (0x00 << 4) | first_id
    device.replies = [Verbatim(bytes(stale))]
    return driver._feature_index(device, 0x01, logitech.FEATURE_BATTERY_STATUS)


def test_a_late_root_reply_cannot_resolve_the_next_feature():
    """The root reply for 0x1004 must not satisfy the query for 0x1000.

    Both requests are byte-identical in every field the matcher can see --
    same device index, same feature index 0, same function 0 -- and the root
    reply does not echo which feature was asked about. The rotating software
    id is the only thing telling them apart.
    """
    driver = logitech.LogitechDriver()
    index = _resolve_with_a_late_reply_from_the_previous_query(driver)
    assert index is None, "a stale root reply was accepted as this feature's index"


def test_without_the_rotation_the_stale_reply_is_accepted(monkeypatch):
    """Pins the mechanism: with one fixed software id -- as this driver used
    to send -- the very same sequence hands 0x1000 the index belonging to
    0x1004. Everything downstream of that is the 15%."""
    monkeypatch.setattr(logitech, "next_software_id", lambda: logitech.SOFTWARE_ID)
    driver = logitech.LogitechDriver()
    assert _resolve_with_a_late_reply_from_the_previous_query(driver) == 6


def test_two_features_never_share_one_index():
    """Belt and braces: even if a stale reply slips through, 0x1000 inheriting
    0x1004's index is structurally impossible in HID++, so the route is
    rejected rather than used to call the wrong function."""
    driver = logitech.LogitechDriver()

    device = FakeDevice(
        replies=[
            # 0x1004 resolves to index 6...
            _reply(0x01, 0x00, 0x00, bytes([6, 0, 5])),
            # ...but its getStatus goes unanswered, so discovery falls through.
            [],
            # 0x1000 then "resolves" to the same index 6 -- the desync.
            _reply(0x01, 0x00, 0x00, bytes([6, 0, 5])),
            # If the guard failed, this capabilities reply would be read as 15%.
            _reply(0x01, 0x06, 0x00, UNIFIED_CAPABILITIES),
        ]
    )

    assert driver._discover_route(device, 0x01) is None


def test_an_unrelated_error_report_does_not_abort_the_transaction():
    """An error addressed to a different request used to return None from the
    middle of ours, sending discovery down the fallback path with a stale
    reply already waiting for it -- the first domino in this whole failure."""
    device = FakeDevice()
    driver = logitech.LogitechDriver()

    other_error = bytearray(logitech.LEN_LONG)
    other_error[0] = logitech.ERROR_HIDPP20
    other_error[1] = 0x02  # a different device index entirely
    other_error[2] = 0x06
    other_error[3] = (0x01 << 4) | 0x05
    other_error[4] = logitech.ERR_RESOURCE
    device.queued = [bytes(other_error)]
    device.replies = [_reply(0x01, 0x06, 0x01, UNIFIED_STATUS)]

    reading = driver._call_feature(device, 0x01, 0x06, logitech.UNIFIED)
    assert reading == Reading(online=True, percent=75, charging=False)


def test_software_id_rotates_and_stays_in_range():
    seen = {logitech.next_software_id() for _ in range(64)}
    assert seen == set(logitech.SOFTWARE_IDS)
    # 0 is reserved for device-initiated notifications; sending it would make
    # our own requests indistinguishable from the device's broadcasts.
    assert 0 not in seen
    assert max(seen) <= 15


def test_charging_enums_are_not_shared_between_features():
    """0x1004 status 4 is a charge *error*; 0x1000 status 4 is a slow
    recharge. One shared set had to be wrong for one of them."""
    assert 4 in logitech.LEGACY_CHARGING_STATES
    assert 4 not in logitech.UNIFIED_CHARGING_STATES

    charge_error = logitech.parse_unified_battery(bytes([50, 0, 4, 0]))
    assert charge_error.charging is False

    slow_recharge = logitech.parse_battery_status(bytes([50, 0, 4]))
    assert slow_recharge.charging is True
