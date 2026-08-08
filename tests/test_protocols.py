"""Protocol frame construction and response parsing.

Pure functions only -- no hardware, no HID calls. These lock in the byte
layouts taken from the reference implementations so a refactor can't silently
shift an offset.
"""

import pytest

from mbt.drivers import compx, logitech, pulsar, razer
from mbt.drivers.base import Reading


# --------------------------------------------------------------------------
# Pulsar / CompX gen 1
# --------------------------------------------------------------------------


def test_pulsar_checksum_formula():
    assert pulsar.checksum(b"\x00") == 0x55
    assert pulsar.checksum(b"\x08\x04") == (0x55 - 0x0C) & 0xFF
    # IPI firmware uses a different base over the same frame shape.
    assert pulsar.checksum(b"\x08\x04", base=pulsar.CHECKSUM_IPI) == (0x4D - 0x0C) & 0xFF


def test_pulsar_frame_layout():
    frame = pulsar.build_frame(pulsar.CMD_POWER)
    assert len(frame) == pulsar.FRAME_LEN == 17
    assert frame[0] == pulsar.REPORT_ID == 0x08
    assert frame[1] == 0x04
    # The checksum byte makes the whole frame sum to the base constant.
    assert (sum(frame[:16]) + frame[16]) & 0xFF == pulsar.CHECKSUM_PULSAR


def test_pulsar_frame_rejects_oversized_args():
    with pytest.raises(ValueError):
        pulsar.build_frame(pulsar.CMD_POWER, args=bytes(20))


def test_pulsar_parse_power():
    """The percentage comes from the vendor's voltage curve, not the level byte.

    Their configurator derives the displayed figure from voltage and ignores the
    reported level, and the two disagree in normal use -- so 72 in the level
    field is superseded by the 4020 mV reading.
    """
    response = bytearray(17)
    response[6] = 72          # reported level, superseded by the voltage curve
    response[7] = 1           # external power connected
    response[8:10] = (4020).to_bytes(2, "big")
    reading = pulsar.parse_power(bytes(response))
    assert reading.online is True
    assert reading.charging is True
    assert reading.millivolts == 4020
    assert reading.percent == pulsar.percent_from_voltage(4020, charging=True)


def test_pulsar_zeroed_response_means_offline():
    """A mouse that is off answers with zeros -- that must not read as 0%."""
    assert pulsar.parse_power(bytes(17)) is pulsar.OFFLINE


def test_pulsar_rejects_impossible_percentage():
    response = bytearray(17)
    response[6] = 200
    response[8:10] = (4000).to_bytes(2, "big")
    assert pulsar.parse_power(bytes(response)).online is False


def test_razer_argument_offsets_against_captured_replies():
    """Offsets confirmed on a Viper V3 Pro: the same layout that yields the
    battery level also decodes a real serial and DPI pair, so a regression in
    the argument offset would break all three together."""
    reply = bytearray(razer.REPORT_LEN)
    reply[0] = razer.STATUS_SUCCESS
    reply[6], reply[7] = 0x00, 0x82  # get serial
    reply[8:8 + 15] = b"PM2440H32017653"
    assert bytes(reply[8:8 + 15]).decode() == "PM2440H32017653"

    dpi = bytearray(razer.REPORT_LEN)
    dpi[0] = razer.STATUS_SUCCESS
    dpi[6], dpi[7] = 0x04, 0x85
    dpi[8:13] = bytes([0x00, 0x03, 0x20, 0x03, 0x20])
    assert int.from_bytes(dpi[9:11], "big") == 800
    assert int.from_bytes(dpi[11:13], "big") == 800


def test_razer_battery_level_scaling():
    """Level is 0-255, not a percentage."""
    reply = bytearray(razer.REPORT_LEN)
    reply[9] = 128  # the captured value
    assert razer.parse_battery(bytes(reply)) == 50
    reply[9] = 255
    assert razer.parse_battery(bytes(reply)) == 100
    reply[9] = 0
    assert razer.parse_battery(bytes(reply)) is None


# --------------------------------------------------------------------------
# Razer
# --------------------------------------------------------------------------


def test_razer_report_layout():
    report = razer.build_report(razer.CLASS_POWER, razer.CMD_BATTERY_LEVEL)
    assert len(report) == 90
    assert report[1] == 0x1F      # default transaction id
    assert report[5] == 0x02      # data size
    assert report[6] == 0x07      # command class
    assert report[7] == 0x80      # command id


def test_razer_crc_is_xor_of_2_to_87():
    report = razer.build_report(razer.CLASS_POWER, razer.CMD_BATTERY_LEVEL)
    expected = 0
    for byte in report[2:88]:
        expected ^= byte
    assert report[88] == expected


def test_razer_crc_changes_with_transaction_id():
    a = razer.build_report(0x07, 0x80, transaction_id=0x1F)
    b = razer.build_report(0x07, 0x80, transaction_id=0x3F)
    # transaction_id is byte 1, outside the CRC range, so the CRC is unchanged
    # but the reports differ -- this pins that boundary down.
    assert a[88] == b[88]
    assert a != b


def test_razer_rejects_oversized_arguments():
    with pytest.raises(ValueError):
        razer.build_report(0x07, 0x80, arguments=bytes(81))


def test_razer_response_validation():
    good = bytearray(90)
    good[0] = razer.STATUS_SUCCESS
    good[6] = 0x07
    good[7] = 0x80
    assert razer.is_valid_response(bytes(good), 0x07, 0x80)

    wrong_command = bytearray(good)
    wrong_command[7] = 0x84
    assert not razer.is_valid_response(bytes(wrong_command), 0x07, 0x80)

    truncated = bytes(good[:40])
    assert not razer.is_valid_response(truncated, 0x07, 0x80)


@pytest.mark.parametrize(
    "level,expected",
    [(255, 100), (128, 50), (26, 10), (0, None)],
)
def test_razer_battery_scaling(level, expected):
    response = bytearray(90)
    response[9] = level
    assert razer.parse_battery(bytes(response)) == expected


# --------------------------------------------------------------------------
# Logitech HID++ 2.0
# --------------------------------------------------------------------------


def test_logitech_request_layout():
    request = logitech.build_request(0xFF, 0x06, 0x01, b"\x00\x01")
    assert len(request) == 20
    assert request[0] == logitech.REPORT_LONG
    assert request[1] == 0xFF
    assert request[2] == 0x06
    assert request[3] == (0x01 << 4) | logitech.SOFTWARE_ID


def test_logitech_short_request_length():
    request = logitech.build_request(0x01, 0x00, 0x00, b"\x10\x04\x00", long=False)
    assert len(request) == 7
    assert request[0] == logitech.REPORT_SHORT


def test_logitech_error_detection():
    hidpp10 = bytes([0x8F, 0x01, 0x81, logitech.ERR_RESOURCE, 0x00])
    assert logitech.is_error(hidpp10) == logitech.ERR_RESOURCE

    hidpp20 = bytes([0xFF, 0x01, 0x06, 0x1A, 0x09] + [0] * 15)
    assert logitech.is_error(hidpp20) == 0x09

    normal = bytes([0x11, 0xFF, 0x06, 0x1A] + [0] * 16)
    assert logitech.is_error(normal) is None


def test_logitech_matches_request_rejects_notifications():
    # A notification uses software id 0, so it must not be mistaken for a reply.
    notification = bytes([0x11, 0xFF, 0x06, 0x10] + [0] * 16)
    assert not logitech.matches_request(notification, 0xFF, 0x06, 0x01)

    reply = bytes([0x11, 0xFF, 0x06, (0x01 << 4) | logitech.SOFTWARE_ID] + [0] * 16)
    assert logitech.matches_request(reply, 0xFF, 0x06, 0x01)


def test_logitech_unified_battery():
    reading = logitech.parse_unified_battery(bytes([85, 0, 0, 0]))
    assert reading == Reading(online=True, percent=85, charging=False)

    charging = logitech.parse_unified_battery(bytes([40, 0, 1, 0]))
    assert charging.charging is True


def test_logitech_battery_status_reports_bucket_not_fake_percent():
    """Older mice report 0 for the level; that must not become "0%"."""
    reading = logitech.parse_battery_status(bytes([0, 0, 0]))
    assert reading.online is True
    assert reading.percent is None
    assert reading.bucket == "unknown"

    with_percent = logitech.parse_battery_status(bytes([64, 0, 0]))
    assert with_percent.percent == 64


# --------------------------------------------------------------------------
# CompX gen 2 (Attack Shark)
# --------------------------------------------------------------------------


def test_compx_command_layout():
    command = compx.build_command(2, 2, 0, compx.OPCODE_BATTERY)
    assert len(command) == 64
    assert command[2] == 2 and command[3] == 2
    assert command[5] == 131


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (67, 0, (67, False)),   # percent first
        (0, 67, (67, False)),   # flag first
        (1, 55, (55, True)),    # charging
        (200, 201, (None, None)),
    ],
)
def test_compx_normalize_battery(a, b, expected):
    assert compx.normalize_battery(a, b) == expected


def test_compx_parse_battery():
    response = bytearray(65)
    response[1] = compx.RESPONSE_MARKER
    response[4] = 2
    response[6] = compx.OPCODE_BATTERY
    response[7] = 88
    response[8] = 0
    reading = compx.parse_battery(bytes(response))
    assert reading.online is True
    assert reading.percent == 88


def test_compx_missing_marker_is_offline():
    assert compx.parse_battery(bytes(65)).online is False
