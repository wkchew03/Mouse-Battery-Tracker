"""CompX generation-2 protocol, verified on a CRDRAKO KO-ONE (373e:006b).

The vendor's own panel reported 100% at the moment of capture.
"""

from mbt.drivers import compx
from mbt.drivers.base import Reading


def _pad(head, length=65):
    return bytes(head) + bytes(length - len(head))


# Captured from the device at 100%, not charging. Note this came from a plain
# feature *read* with no command sent -- the device retains the last reply.
BATTERY_AT_100 = _pad([0x00, 0xA1, 0x00, 0x02, 0x02, 0x00, 0x83, 0x00, 0x64])


def test_parses_captured_reply():
    reading = compx.parse_battery(BATTERY_AT_100)
    assert reading == Reading(online=True, percent=100, charging=False)


def test_marker_and_opcode_are_required():
    """0xA1 at [1] and the echoed opcode at [6] are what make this a battery
    reply rather than some other message."""
    bad_marker = bytearray(BATTERY_AT_100)
    bad_marker[1] = 0x00
    assert compx.parse_battery(bytes(bad_marker)) is compx.OFFLINE

    bad_opcode = bytearray(BATTERY_AT_100)
    bad_opcode[6] = 0x10
    assert compx.parse_battery(bytes(bad_opcode)) is compx.OFFLINE


def test_charging_flag():
    charging = bytearray(BATTERY_AT_100)
    charging[7] = 1
    charging[8] = 80
    assert compx.parse_battery(bytes(charging)) == Reading(
        online=True, percent=80, charging=True
    )


def test_normalize_handles_either_byte_order():
    """Firmware disagrees on whether percent or the flag comes first."""
    assert compx.normalize_battery(0, 100) == (100, False)
    assert compx.normalize_battery(100, 0) == (100, False)
    assert compx.normalize_battery(1, 45) == (45, True)


def test_command_layout():
    command = compx.build_command(2, 2, 0, compx.OPCODE_BATTERY)
    assert len(command) == compx.BUFFER_LEN == 64
    assert command[2] == 2 and command[3] == 2
    assert command[5] == compx.OPCODE_BATTERY == 131


# --------------------------------------------------------------------------
# Collection selection -- the bug that hid this device
# --------------------------------------------------------------------------


def _info(usage_page, path, usage=1, interface=1):
    return {
        "vendor_id": compx.VENDOR_ATTACK_SHARK,
        "product_id": 0x006B,
        "usage_page": usage_page,
        "usage": usage,
        "interface_number": interface,
        "path": path,
        "serial_number": "C0C6FFD410D12C6F",
    }


def test_selects_the_feature_channel_regardless_of_usage_page(monkeypatch):
    """The KO-ONE puts the command channel on 0xffff, not 0xff00. Selecting by
    usage page alone skipped the device completely."""
    feature_path = b"has-feature"
    monkeypatch.setattr(
        compx, "has_feature_channel", lambda info: info["path"] == feature_path
    )
    infos = [
        _info(0xFFA0, b"input-only-a"),
        _info(0xFFFF, b"input-only-b"),
        _info(0xFFFF, feature_path, usage=0, interface=2),
    ]
    picked = compx.CompxDriver().candidates(infos)
    assert [i["path"] for i in picked] == [feature_path]


def test_ignores_collections_without_a_feature_channel(monkeypatch):
    monkeypatch.setattr(compx, "has_feature_channel", lambda info: False)
    assert compx.CompxDriver().candidates([_info(0xFF00, b"x")]) == []


def test_ignores_non_vendor_pages(monkeypatch):
    monkeypatch.setattr(compx, "has_feature_channel", lambda info: True)
    assert compx.CompxDriver().candidates([_info(0x0001, b"m", usage=2)]) == []


# --------------------------------------------------------------------------
# G-Wolves shares the platform under a different vendor id
# --------------------------------------------------------------------------

# Captured from a G-Wolves HTS Ultra 8K receiver (33e4:0017) at 100%.
GWOLVES_AT_100 = _pad([0x00, 0xA1, 0x00, 0x02, 0x02, 0x00, 0x83, 0x00, 0x64])


def test_gwolves_reply_parses_identically():
    """Same firmware platform, same 0xA1 frame -- only the vendor id differs."""
    assert compx.parse_battery(GWOLVES_AT_100) == Reading(
        online=True, percent=100, charging=False
    )


def test_gwolves_vendor_is_claimed(monkeypatch):
    monkeypatch.setattr(compx, "has_feature_channel", lambda info: True)
    info = _info(0xFFFF, b"gw", usage=0, interface=2)
    info["vendor_id"] = compx.VENDOR_GWOLVES
    info["product_id"] = 0x0017
    assert compx.CompxDriver().candidates([info])


def test_unrelated_vendor_is_not_claimed(monkeypatch):
    monkeypatch.setattr(compx, "has_feature_channel", lambda info: True)
    info = _info(0xFFFF, b"other", usage=0, interface=2)
    info["vendor_id"] = 0x1532  # Razer
    assert compx.CompxDriver().candidates([info]) == []
