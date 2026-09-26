"""VAXEE protocol, verified on an XE-S Wireless (3057:2001).

Constants come from the vendor's own web driver: `paramter.js` for the frame
header and report id, `api.js` for the command ids.
"""

from mbt.drivers import vaxee
from mbt.drivers.base import Reading


def _pad(head, length=64):
    return bytes(head) + bytes(length - len(head))


# Captured live: raw step 0x13 = 19 -> 95%, not charging.
LEVEL_AT_95 = _pad([0x0E, 0xA5, 0x0B, 0x01, 0x01, 0x13, 0x1E, 0x10, 0x13])
CHARGING_REPLY = _pad([0x0E, 0xA5, 0x10, 0x01, 0x01, 0x00])


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------


def test_command_layout():
    command = vaxee.build_command(vaxee.CMD_BATTERY_LEVEL)
    assert len(command) == vaxee.PAYLOAD_LEN == 63
    assert command[0] == vaxee.HEADER == 0xA5
    assert command[1] == vaxee.CMD_BATTERY_LEVEL == 0x0B
    assert command[2] == vaxee.READ == 0x01
    assert command[3] == vaxee.DATA_LENGTH


def test_charging_command_differs_only_by_id():
    level = vaxee.build_command(vaxee.CMD_BATTERY_LEVEL)
    charge = vaxee.build_command(vaxee.CMD_CHARGING_STATUS)
    assert charge[1] == 0x10
    assert level[0] == charge[0]
    assert level[2:] == charge[2:]


# --------------------------------------------------------------------------
# The 0-20 scale -- the part that would silently misreport
# --------------------------------------------------------------------------


def test_level_is_a_step_not_a_percentage():
    """Raw 19 means 95%, not 19%. The vendor's tables are keyed _0.._100 in
    fives, which is what fixes the scale."""
    assert vaxee.parse_level(LEVEL_AT_95) == 95


def test_scale_endpoints():
    assert vaxee.parse_level(_pad([0, 0, 0, 0, 0, 20])) == 100
    assert vaxee.parse_level(_pad([0, 0, 0, 0, 0, 0])) == 0
    assert vaxee.parse_level(_pad([0, 0, 0, 0, 0, 10])) == 50


def test_value_above_the_scale_is_rejected():
    """A raw 95 would be 475% -- a reply we have misunderstood, not a battery."""
    assert vaxee.parse_level(_pad([0, 0, 0, 0, 0, 95])) is None


def test_short_reply_has_no_level():
    assert vaxee.parse_level(b"\x0e\xa5\x0b") is None


# --------------------------------------------------------------------------
# Reply validation
# --------------------------------------------------------------------------


def test_valid_reply_accepted():
    assert vaxee.is_valid_reply(LEVEL_AT_95, vaxee.CMD_BATTERY_LEVEL)


def test_zero_second_byte_is_a_failure():
    """The vendor's own success check is `response[1] != 0`."""
    bad = bytearray(LEVEL_AT_95)
    bad[1] = 0
    assert not vaxee.is_valid_reply(bytes(bad), vaxee.CMD_BATTERY_LEVEL)


def test_reply_for_another_command_is_rejected():
    """Guards against reading the charging reply as a battery level."""
    assert not vaxee.is_valid_reply(CHARGING_REPLY, vaxee.CMD_BATTERY_LEVEL)
    assert vaxee.is_valid_reply(CHARGING_REPLY, vaxee.CMD_CHARGING_STATUS)


# --------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------


def _info(usage_page, pid=0x2001, vid=vaxee.VENDOR_VAXEE):
    return {
        "vendor_id": vid,
        "product_id": pid,
        "usage_page": usage_page,
        "usage": 0x01,
        "interface_number": 1,
        "path": f"p{usage_page}".encode(),
        "serial_number": "",
    }


def test_selects_the_command_collection():
    """Three vendor collections are exposed; 0xff05 carries report 0x0e."""
    infos = [_info(0xFF00), _info(0xFF90), _info(0xFF05)]
    picked = vaxee.VaxeeDriver().candidates(infos)
    assert [i["usage_page"] for i in picked] == [vaxee.VENDOR_USAGE_PAGE]


def test_unknown_product_is_not_claimed():
    assert vaxee.VaxeeDriver().candidates([_info(0xFF05, pid=0x9999)]) == []


def test_other_vendors_are_not_claimed():
    assert vaxee.VaxeeDriver().candidates([_info(0xFF05, vid=0x1532)]) == []
