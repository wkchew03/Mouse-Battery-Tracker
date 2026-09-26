"""IPI `ms_pix_v1` frame construction and get_basic_info parsing.

Fixtures are real bytes captured from an IPI Float 88 (`372e:1014`), so these
lock in offsets that were confirmed against the vendor's own configurator
rather than inferred.
"""

from mbt.drivers import ipi
from mbt.drivers.base import Reading

# Captured from the device with the web configurator showing the same value.
BASIC_INFO_AT_80 = bytes(
    [0x03, 0x50, 0x00, 0x0B, 0x02, 0x01, 0x50, 0x21, 0x07, 0x01, 0x90, 0x65, 0x04,
     0xA5, 0x5A, 0x00]
)

# The short status reply returned by the *first* read after sending a command.
STUB = bytes([0x03, 0x50, 0x02, 0x00, 0x02])


# --------------------------------------------------------------------------
# Checksum and frame construction
# --------------------------------------------------------------------------


def test_set_crc_matches_vendor_constants():
    """The bundle ships precomputed frames; our checksum must reproduce them."""
    # pct (get_uuid): [152, 80, 0, 1, 71, 0]
    assert ipi.set_crc([0, 80, 0, 1, 71, 0])[0] == 152
    # gct: [233, 80, 0, 10, 79, 64]
    assert ipi.set_crc([0, 80, 0, 10, 79, 64])[0] == 233
    # fct: [33, 80, 0, 2, 79, 128] -- 289 wraps to 33
    assert ipi.set_crc([0, 80, 0, 2, 79, 128])[0] == 33


def test_set_crc_does_not_mutate_input():
    original = [0, 80, 0, 2, 79, 129]
    ipi.set_crc(original)
    assert original[0] == 0


def test_build_command_pads_and_checksums():
    frame = ipi.build_command(ipi.CMD_BASIC_INFO)
    assert len(frame) == ipi.PAYLOAD_LEN == 63
    assert frame[1] == ipi.FRAME_MARKER == 0x50
    # Checksum covers the whole padded payload, not just the command bytes.
    assert frame[0] == sum(frame[1:]) % 256
    assert frame[0] == 34  # 80 + 0 + 2 + 79 + 129 = 290 -> 34


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def test_parse_basic_info_reads_captured_battery():
    assert ipi.parse_basic_info(BASIC_INFO_AT_80) == Reading(online=True, percent=80)


def test_stub_reply_is_not_a_reading():
    """The 5-byte stub must not parse -- it is 'still working', not 'offline data'."""
    assert ipi.parse_basic_info(STUB) is ipi.OFFLINE


def test_parse_rejects_wrong_marker():
    bad = bytearray(BASIC_INFO_AT_80)
    bad[1] = 0x00
    assert ipi.parse_basic_info(bytes(bad)) is ipi.OFFLINE


def test_parse_rejects_mismatched_command_echo():
    bad = bytearray(BASIC_INFO_AT_80)
    bad[4] = 0x09
    assert ipi.parse_basic_info(bytes(bad)) is ipi.OFFLINE


def test_charging_sentinel_is_not_a_percentage():
    """0xE4 means "on external power" -- it must not read as 228% or as off.

    Captured from hardware while charging. Reporting the mouse as offline here
    was a real bug: a charging mouse vanished from "Connected".
    """
    charging = bytearray(BASIC_INFO_AT_80)
    charging[6] = ipi.CHARGING_SENTINEL
    reading = ipi.parse_basic_info(bytes(charging))
    assert reading.online is True
    assert reading.charging is True
    assert reading.percent is None
    assert reading.describe() == "charging"


def test_out_of_range_value_still_counts_as_present():
    """A validated frame means the device answered, whatever the level says."""
    odd = bytearray(BASIC_INFO_AT_80)
    odd[6] = 200
    reading = ipi.parse_basic_info(bytes(odd))
    assert reading.online is True
    assert reading.percent is None


# --------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------


def _info(pid, usage_page, interface=2):
    return {
        "vendor_id": ipi.VENDOR_IPI,
        "product_id": pid,
        "usage_page": usage_page,
        "usage": 0x01,
        "interface_number": interface,
        "path": b"test",
        "serial_number": "000000000001",
    }


def test_candidates_picks_vendor_collection_only():
    infos = [
        _info(0x1014, 0x0001, interface=0),  # mouse collection
        _info(0x1014, 0x000C, interface=2),  # consumer
        _info(0x1014, 0xFF06, interface=2),  # wrong vendor page
        _info(0x1014, 0xFF00, interface=2),  # the one that answers
    ]
    picked = ipi.IpiDriver().candidates(infos)
    assert [i["usage_page"] for i in picked] == [0xFF00]


def test_candidates_returns_one_entry_per_device():
    infos = [_info(0x1014, 0xFF00), _info(0x1015, 0xFF00)]
    picked = ipi.IpiDriver().candidates(infos)
    assert {i["product_id"] for i in picked} == {0x1014, 0x1015}


def test_candidates_ignores_other_vendors():
    other = _info(0x1014, 0xFF00)
    other["vendor_id"] = 0x1532
    assert ipi.IpiDriver().candidates([other]) == []
