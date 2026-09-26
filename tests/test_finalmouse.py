"""Finalmouse UltralightX framing, from bytes captured on a 361d:0100 dongle.

Pure functions only -- no hardware, no HID calls. The frame layout and the
battery parse come from Finalmouse's own XPanel WebHID bundle, so these lock
in what that client does rather than what looked plausible.
"""

from mbt.drivers import finalmouse
from mbt.drivers.base import Reading, resolve_label

DRIVER = finalmouse.FinalmouseDriver()


def _info(usage_page=0xFF00, product_id=0x0100, serial="A3613285AFBE9536"):
    return {
        "vendor_id": finalmouse.VENDOR_FINALMOUSE,
        "product_id": product_id,
        "usage_page": usage_page,
        "usage": 0x0001,
        "interface_number": 0,
        "path": f"ulx-{usage_page:04x}-{product_id:04x}".encode(),
        "serial_number": serial,
        "manufacturer_string": "Finalmouse",
        "product_string": "Finalmouse UltralightX dongle",
    }


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_request_frame_matches_the_vendor_client():
    """XPanel builds `[2 + arglen, 0x80 | cmd, arglen]` on report 4. This is
    the exact frame that drew 3956 mV out of the dongle."""
    frame = finalmouse.build_frame(finalmouse.CMD_VBAT)
    assert len(frame) == 64
    assert frame[0] == finalmouse.REPORT_OUT == 0x04
    assert frame[1] == 2
    assert frame[2] == 0x85  # 0x80 | 0x05
    assert frame[3] == 0
    assert set(frame[4:]) == {0}


def test_request_frame_carries_arguments():
    frame = finalmouse.build_frame(0x10, b"\x40\x06")
    assert frame[1] == 4  # 2 + 2
    assert frame[2] == 0x90
    assert frame[3] == 2
    assert frame[4:6] == b"\x40\x06"


def test_oversized_arguments_are_refused():
    import pytest

    with pytest.raises(ValueError):
        finalmouse.build_frame(finalmouse.CMD_VBAT, bytes(80))


# --------------------------------------------------------------------------
# Matching -- the RSSI broadcast is the reason this is not "next report wins"
# --------------------------------------------------------------------------


# Captured unsolicited from the dongle, several per second while the link is
# up: CMD_ID_RSSI with one signed byte. 0xe7 is -25 dBm.
RSSI_BROADCAST = bytes([0x05, 0x03, 0x0D, 0x01, 0xE7]) + bytes(59)

# Captured reply to CMD_ID_VBAT: 0x0f74 little-endian = 3956 mV.
VBAT_REPLY = bytes([0x05, 0x04, 0x05, 0x02, 0x74, 0x0F]) + bytes(58)

# Captured reply to CMD_ID_BATTERY_CHARGING with the mouse on battery.
CHARGING_REPLY = bytes([0x05, 0x03, 0x25, 0x01, 0x00]) + bytes(59)


def test_rssi_broadcast_is_not_mistaken_for_a_reply():
    """The trap this device sets: it volunteers RSSI constantly, so a driver
    that takes the next report reports link strength as a battery level."""
    assert not finalmouse.matches_reply(RSSI_BROADCAST, finalmouse.CMD_VBAT)
    assert not finalmouse.matches_reply(
        RSSI_BROADCAST, finalmouse.CMD_BATTERY_STATUS
    )
    assert finalmouse.matches_reply(RSSI_BROADCAST, finalmouse.CMD_RSSI)


def test_reply_matching_requires_the_input_report():
    outgoing = finalmouse.build_frame(finalmouse.CMD_VBAT)
    assert not finalmouse.matches_reply(outgoing, finalmouse.CMD_VBAT)


def test_reply_body_is_trimmed_to_its_declared_length():
    assert finalmouse.reply_body(VBAT_REPLY) == b"\x74\x0f"
    assert finalmouse.reply_body(CHARGING_REPLY) == b"\x00"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_voltage_is_little_endian():
    """Captured at 3956 mV. XPanel reads it as `t[e] | t[e+1] << 8`."""
    assert finalmouse.parse_voltage(finalmouse.reply_body(VBAT_REPLY)) == 3956


def test_battery_status_carries_charge_then_voltage():
    """XPanel parses this reply as {soc: data[0], voltage_mv: u16le(data, 1)}."""
    body = bytes([76, 0x74, 0x0F])
    assert finalmouse.parse_battery_status(body) == Reading(
        online=True, percent=76, millivolts=3956
    )


def test_battery_status_out_of_range_reports_presence_without_a_level():
    """The device answered, so it is awake -- but no number is invented from
    a value that is not a percentage."""
    reading = finalmouse.parse_battery_status(bytes([200, 0x74, 0x0F]))
    assert reading.online is True
    assert reading.percent is None
    assert reading.millivolts == 3956


def test_battery_status_needs_three_bytes():
    assert finalmouse.parse_battery_status(b"\x4c") is finalmouse.OFFLINE


def test_link_state_zero_is_not_linked():
    """Zero is what puts XPanel into 'waiting for the mouse'."""
    assert finalmouse.is_linked(b"\x01") is True
    assert finalmouse.is_linked(b"\x00") is False
    assert finalmouse.is_linked(b"") is False


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_only_the_vendor_collection_is_claimed():
    mouse = _info(usage_page=0x0001)
    vendor = _info(usage_page=0xFF00)
    assert DRIVER.candidates([mouse, vendor]) == [vendor]


def test_every_published_product_id_is_covered():
    """Finalmouse's own udev rules list ten ids; a driver that knew only the
    one dongle in hand would miss the rest of the family."""
    for pid in finalmouse.PRODUCT_IDS:
        assert DRIVER.candidates([_info(product_id=pid)]), f"{pid:#06x} not claimed"


def test_an_unrelated_finalmouse_id_is_not_claimed():
    assert DRIVER.candidates([_info(product_id=0x0999)]) == []


def test_one_entry_per_device():
    """Two collections of the same dongle must not become two mice."""
    first = _info(usage_page=0xFF00)
    second = dict(first)
    second["path"] = b"another-collection"
    assert len(DRIVER.candidates([first, second])) == 1


def test_label_names_the_model():
    """Every variant reports the same product string, so the model has to come
    from the product id."""
    assert resolve_label(DRIVER, _info()) == "Finalmouse UltralightX"


def test_unknown_model_keeps_the_usb_strings():
    assert DRIVER.label(_info(product_id=0x0203)) is None


# --------------------------------------------------------------------------
# No percentage is available on this firmware
# --------------------------------------------------------------------------


def test_voltage_is_surfaced_as_a_level_not_converted():
    """CMD_ID_BATTERY_STATUS never answers on the observed dongle, so voltage
    is all there is. Turning it into a percentage would need Finalmouse's
    discharge curve, which this app does not have."""
    assert finalmouse.format_volts(3956) == "3.96 V"
    assert finalmouse.format_volts(4012) == "4.01 V"


def test_a_voltage_reading_carries_no_invented_percentage():
    reading = Reading(
        online=True,
        millivolts=3956,
        bucket=finalmouse.format_volts(3956),
        connection="2.4 GHz",
    )
    assert reading.percent is None
    assert reading.describe() == "3.96 V"
