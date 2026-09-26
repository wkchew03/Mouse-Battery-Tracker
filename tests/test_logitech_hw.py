"""Logitech HID++, verified on a G Pro X Superlight 2 via a Lightspeed receiver.

The device answers on receiver slot 0x01 rather than the direct-connect index
0xFF, and resolves feature 0x1004 (UNIFIED_BATTERY) to index 6.
"""

from mbt.drivers import logitech
from mbt.drivers.base import Reading

DEVICE_INDEX = 0x01
FEATURE_INDEX = 6


def _reply(params: bytes, feature_index: int = FEATURE_INDEX, function: int = 0x01):
    """A long HID++ reply carrying `params`."""
    frame = bytearray(logitech.LEN_LONG)
    frame[0] = logitech.REPORT_LONG
    frame[1] = DEVICE_INDEX
    frame[2] = feature_index
    frame[3] = (function << 4) | logitech.SOFTWARE_ID
    frame[4 : 4 + len(params)] = params
    return bytes(frame)


def test_unified_battery_reports_a_real_percentage():
    """0x1004 gives a state of charge, so no bucket guessing is needed."""
    reading = logitech.parse_unified_battery(bytes([91, 0, 0, 0]))
    assert reading == Reading(online=True, percent=91, charging=False)


def test_unified_battery_charging_flag():
    charging = logitech.parse_unified_battery(bytes([80, 0, 1, 1]))
    assert charging.percent == 80
    assert charging.charging is True


def test_reply_matching_accepts_our_own_request():
    """Devices emit unsolicited notifications on the same channel, so replies
    are matched on device/feature/function rather than 'next report wins'."""
    assert logitech.addresses_request(
        _reply(bytes([91, 0, 0, 0])), DEVICE_INDEX, FEATURE_INDEX, 0x01
    )


def test_reply_matching_rejects_another_devices_notification():
    other = bytearray(_reply(bytes([50, 0, 0, 0])))
    other[1] = 0x02  # different receiver slot
    assert not logitech.addresses_request(
        bytes(other), DEVICE_INDEX, FEATURE_INDEX, 0x01
    )


def test_reply_matching_rejects_a_different_feature():
    assert not logitech.addresses_request(
        _reply(bytes([91, 0, 0, 0]), feature_index=9), DEVICE_INDEX, FEATURE_INDEX, 0x01
    )


def test_request_carries_the_software_id():
    """Without the software id our replies are indistinguishable from
    notifications the device sends on its own."""
    request = logitech.build_request(DEVICE_INDEX, FEATURE_INDEX, 0x01)
    assert request[3] & 0x0F == logitech.SOFTWARE_ID


def test_error_reply_is_detected():
    """An error report is identified by its report id, not by a field inside a
    normal reply: HID++ 1.0 errors arrive as 0x8f with the code at index 3."""
    frame = bytearray(logitech.LEN_SHORT)
    frame[0] = logitech.ERROR_HIDPP10
    frame[1] = DEVICE_INDEX
    frame[3] = logitech.ERR_RESOURCE
    assert logitech.is_error(bytes(frame)) == logitech.ERR_RESOURCE


def test_normal_reply_is_not_an_error():
    assert logitech.is_error(_reply(bytes([91, 0, 0, 0]))) is None
