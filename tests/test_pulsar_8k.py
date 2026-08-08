"""Pulsar 8K platform (3710:5406), verified against an X2 CrazyLight.

Fixtures are real bytes captured from the device, cross-checked against Pulsar's
own WebHID configurator.
"""

from mbt.drivers import pulsar
from mbt.drivers.base import Reading

# Captured live: 95%, not charging, 4118 mV.
RESPONSE_AT_95 = bytes(
    [0x08, 0x04, 0x00, 0x00, 0x00, 0x02, 0x5F, 0x00, 0x10, 0x16, 0x00, 0x00,
     0x00, 0x00, 0x00, 0x00, 0x00]
)


def _info(usage_page, pid=0x5406, vid=pulsar.VENDOR_PULSAR_8K, interface=1):
    return {
        "vendor_id": vid,
        "product_id": pid,
        "usage_page": usage_page,
        "usage": 0x02,
        "interface_number": interface,
        "path": f"p{usage_page}".encode(),
        "serial_number": "522098634735",
    }


# --------------------------------------------------------------------------
# Frame construction
# --------------------------------------------------------------------------


def test_wire_frame_sums_to_the_checksum_base():
    """The vendor's `s[15] = checksum - gt` (gt=8) means the whole wire frame
    including the report ID sums to 0x55."""
    frame = pulsar.build_frame(pulsar.CMD_POWER)
    assert len(frame) == 17
    assert sum(frame) & 0xFF == pulsar.CHECKSUM_PULSAR


def test_battery_frame_matches_captured_bytes():
    assert pulsar.build_frame(pulsar.CMD_POWER) == bytes(
        [0x08, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
         0x00, 0x00, 0x00, 0x00, 0x00, 0x49]
    )


def test_payload_length_and_offset():
    """Payload length goes at [5] and the payload itself at [6:]."""
    frame = pulsar.build_frame(0x07, args=b"\xaa\xbb")
    assert frame[5] == 2
    assert frame[6:8] == b"\xaa\xbb"
    assert sum(frame) & 0xFF == pulsar.CHECKSUM_PULSAR


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def test_parses_captured_response():
    """4118 mV is above the vendor's top table entry (4110), so their software
    shows 100% even though the mouse reports 95. We match the software."""
    reading = pulsar.parse_power(RESPONSE_AT_95)
    assert reading == Reading(
        online=True, percent=100, charging=False, millivolts=4118
    )


# --------------------------------------------------------------------------
# Vendor voltage curve
# --------------------------------------------------------------------------


def test_above_the_table_is_full():
    assert pulsar.percent_from_voltage(4122) == 100
    assert pulsar.percent_from_voltage(4118) == 100


def test_charging_caps_just_below_full():
    """The vendor reports 99 while charging so it does not claim 'done' early."""
    assert pulsar.percent_from_voltage(4122, charging=True) == 99


def test_top_table_entry_is_not_reported_as_empty():
    """The vendor's own function returns 0 at exactly the top entry, because the
    lookup finds no bucket. Reproducing that would show a full cell as flat."""
    assert pulsar.percent_from_voltage(4110) == 100


def test_curve_is_monotonic():
    previous = -1
    for millivolts in range(3000, 4200, 10):
        value = pulsar.percent_from_voltage(millivolts)
        assert value >= previous
        previous = value


def test_curve_spans_the_full_range():
    assert pulsar.percent_from_voltage(3000) == 0
    assert pulsar.percent_from_voltage(4200) == 100
    midpoint = pulsar.percent_from_voltage(3880)
    assert 40 <= midpoint <= 60


def test_level_is_used_when_no_voltage_is_reported():
    """Some replies carry no voltage; the device's own level is the fallback."""
    response = bytearray(RESPONSE_AT_95)
    response[8:10] = b"\x00\x00"
    assert pulsar.parse_power(bytes(response)).percent == 95


def test_voltage_is_big_endian():
    """4118 mV is physically consistent with 95% on a 4.2 V cell; the
    little-endian reading (0x1610 = 5648) is not a possible cell voltage."""
    assert pulsar.parse_power(RESPONSE_AT_95).millivolts == 4118


def test_charging_flag():
    charging = bytearray(RESPONSE_AT_95)
    charging[7] = 1
    assert pulsar.parse_power(bytes(charging)).charging is True


# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

# Captured live: online, device address 0ce4ab.
DEVICE_ONLINE = bytes(
    [0x08, 0x03, 0x00, 0x00, 0x00, 0x01, 0x01, 0xAB, 0xE4, 0x0C, 0x00, 0x00,
     0x00, 0x00, 0x00, 0x00, 0xAD]
)


def test_parses_device_address():
    """The address bytes are stored reversed by the vendor."""
    online, address = pulsar.parse_device_online(DEVICE_ONLINE)
    assert online is True
    assert address == "0ce4ab"


def test_offline_device_reports_no_address():
    reply = bytearray(DEVICE_ONLINE)
    reply[6] = 0
    reply[7:10] = b"\x00\x00\x00"
    online, address = pulsar.parse_device_online(bytes(reply))
    assert online is False
    assert address is None


def test_short_reply_is_not_an_identity():
    assert pulsar.parse_device_online(b"\x08\x03") == (False, None)


def test_identity_is_namespaced():
    """Must not collide with a USB-derived key like '3710:5406:...'."""
    identity = pulsar.device_identity("0ce4ab")
    assert identity == "pulsar:0ce4ab"
    assert ":" in identity


def test_dongle_serial_is_not_used_as_identity():
    """Two different Pulsar dongles were observed reporting the SAME serial
    (522098634735), so the dongle cannot identify the mouse."""
    driver = pulsar.PulsarDriver()
    a = driver._dongle_key(_info(0xFF02))
    b = driver._dongle_key(_info(0xFF02, pid=0x5406))
    assert a == b  # identical dongles are indistinguishable...
    # ...so identity has to come from the device address instead.
    assert pulsar.device_identity("0ce4ab") != pulsar.device_identity("aabbcc")


# --------------------------------------------------------------------------
# Collection selection
# --------------------------------------------------------------------------


def test_selects_the_command_channel():
    """Five vendor collections are exposed and only 0xff02 answers."""
    infos = [
        _info(0xFF03),
        _info(0xFF04),
        _info(0xFF02),
        _info(0xFF05),
        _info(0xFF06),
    ]
    picked = pulsar.PulsarDriver().candidates(infos)
    assert [i["usage_page"] for i in picked] == [pulsar.COMMAND_USAGE_PAGE]


def test_ignores_non_vendor_collections():
    infos = [_info(0x0001, interface=0), _info(0x000C)]
    assert pulsar.PulsarDriver().candidates(infos) == []


def test_accepts_both_pulsar_vendor_ids():
    driver = pulsar.PulsarDriver()
    assert driver.candidates([_info(0xFF02)])
    assert driver.candidates(
        [_info(0xFF02, pid=0xF508, vid=pulsar.VENDOR_PULSAR)]
    )


def test_one_entry_per_physical_device():
    infos = [_info(0xFF02), _info(0xFF02, pid=0x5407)]
    assert len(pulsar.PulsarDriver().candidates(infos)) == 2
