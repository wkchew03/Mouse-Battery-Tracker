"""Orbitalworks protocol, verified against a Pathfinder V1 receiver (1915:0746).

Fixtures are real captured bytes, cross-checked against the vendor's WebHID
configurator, which reported 96% at the moment of capture.
"""

from mbt.drivers import orbital
from mbt.drivers.base import Reading


def _pad(head):
    return bytes(head) + bytes(orbital.PACKET_LEN - len(head))


# Routed power reply captured while the web app showed 96%, on battery.
POWER_AT_96 = _pad(
    [0x41, 0x00, 0x8E, 0x01, 0x00, 0x00, 0x60, 0x00, 0x00, 0x03, 0x06, 0x00,
     0x00, 0x00, 0xFF, 0x20]
)

# Reply the RECEIVER sends to an unrouted request. Not a power reply, but it
# passes every check except the command marker.
NOT_POWER = _pad(
    [0x01, 0x00, 0x8C, 0x01, 0x0A, 0x15, 0x19, 0x46, 0x07, 0x06, 0x01, 0xF3, 0x09]
)


# --------------------------------------------------------------------------
# Packet construction
# --------------------------------------------------------------------------


def test_checksum_reconciles_to_the_vendor_constant():
    packet = orbital.build_packet(orbital.CMD_READ_POWER, receiver=True)
    assert (sum(packet[:63]) + packet[63]) & 0xFF == orbital.CHECKSUM_BASE


def test_receiver_flag_sets_the_routing_bit():
    """0x40 routes the request through to the mouse; without it the receiver
    answers on its own behalf and never asks the mouse."""
    assert orbital.build_packet(orbital.CMD_READ_POWER, receiver=True)[0] == 0x41
    assert orbital.build_packet(orbital.CMD_READ_POWER, receiver=False)[0] == 0x01


def test_packet_layout():
    packet = orbital.build_packet(orbital.CMD_READ_POWER, receiver=False)
    assert len(packet) == 64
    assert packet[2] == orbital.CMD_READ_POWER
    assert packet[3] == 0x01


# --------------------------------------------------------------------------
# Reply validation -- the part that stops silent garbage
# --------------------------------------------------------------------------


def test_accepts_a_real_power_reply():
    assert orbital.is_power_reply(POWER_AT_96, "dms") is True


def test_rejects_the_receivers_non_power_reply():
    """This is the regression that matters. Without the 0x8e marker check this
    reply parses as 21% -- a plausible, entirely wrong number."""
    assert orbital.is_power_reply(NOT_POWER, "dms") is False


def test_short_reply_rejected():
    assert orbital.is_power_reply(b"\x41\x00\x8e\x01", "dms") is False


def test_v2_markers_differ():
    v2 = bytearray(POWER_AT_96)
    v2[2] = 0x99
    assert orbital.is_power_reply(bytes(v2), "dms_v2") is True
    assert orbital.is_power_reply(bytes(v2), "dms") is False


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parses_captured_percentage():
    reading = orbital.parse_power(POWER_AT_96, "dms")
    assert reading.percent == 96
    assert reading.online is True
    assert reading.charging is False


def test_charging_states():
    for state, charging in ((0, False), (1, True), (2, True), (3, True)):
        reply = bytearray(POWER_AT_96)
        reply[5] = state
        assert orbital.parse_power(bytes(reply), "dms").charging is charging


def test_rejects_impossible_percentage():
    reply = bytearray(POWER_AT_96)
    reply[6] = 200
    assert orbital.parse_power(bytes(reply), "dms") is orbital.OFFLINE


def test_v2_offsets_are_five_higher():
    reply = bytearray(orbital.PACKET_LEN)
    reply[10] = 1
    reply[11] = 55
    assert orbital.parse_power(bytes(reply), "dms_v2") == Reading(
        online=True, percent=55, charging=True
    )


# --------------------------------------------------------------------------
# Device selection and identity
# --------------------------------------------------------------------------


def _info(pid, usage_page=orbital.VENDOR_USAGE_PAGE, usage=1):
    return {
        "vendor_id": orbital.VENDOR_ORBITAL,
        "product_id": pid,
        "usage_page": usage_page,
        "usage": usage,
        "interface_number": 3,
        "path": f"p{pid}{usage_page}".encode(),
        "serial_number": "",
    }


def test_receiver_and_wired_share_one_identity():
    driver = orbital.OrbitalDriver()
    assert driver.identity(_info(0x0746)) == driver.identity(_info(0x0747))
    assert driver.label(_info(0x0746)) == "Orbital Pathfinder"


def test_ghost_is_a_separate_mouse():
    driver = orbital.OrbitalDriver()
    assert driver.identity(_info(0x080B)) != driver.identity(_info(0x0746))


def test_candidates_selects_only_the_vendor_collection():
    infos = [_info(0x0746, usage_page=0x0001, usage=2), _info(0x0746)]
    picked = orbital.OrbitalDriver().candidates(infos)
    assert [i["usage_page"] for i in picked] == [orbital.VENDOR_USAGE_PAGE]


def test_finalmouse_vid_collision_is_not_claimed():
    """Finalmouse also uses Nordic's 0x1915; only known Orbital pids match."""
    stray = _info(0xF6B0)
    assert orbital.OrbitalDriver().candidates([stray]) == []
