"""Ninjutso Sora V2 protocol, verified on a receiver (1915:ae1c).

Frames come from the vendor's WebHID configurator at ninjaforce.co/sorav2;
the replies below were captured from the receiver with the mouse at 85%.
"""

from mbt.drivers import ninjutso


def _raw(hex_bytes: str) -> bytes:
    return bytes.fromhex(hex_bytes)


# Captured live. Byte 31 is not checked: it sums for some replies and not others.
BATTERY_85 = _raw(
    "05 15 00 00 01 00 00 04 00 55 00 00 01 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 75"
)
PAIRED_AE11 = _raw(
    "05 28 00 00 01 00 00 02 00 11 ae 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 f1"
)
FIRMWARE_REPLY = _raw(
    "05 09 00 00 01 00 00 04 00 08 01 01 ae 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 cd"
)


def _with(reply: bytes, **offsets: int) -> bytes:
    data = bytearray(reply)
    for offset, value in offsets.items():
        data[int(offset.lstrip("_"))] = value
    return bytes(data)


def _info(pid, usage_page=0xFFA0, **extra):
    return {
        "vendor_id": 0x1915,
        "product_id": pid,
        "usage_page": usage_page,
        "usage": 0x01,
        "interface_number": 1,
        "serial_number": "000000000000",
        "path": b"x",
        **extra,
    }


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


def test_battery_command_matches_the_vendor_frame():
    """The vendor sends [21,0,0,1,0,0,4] on report 5, zero padded to 31 bytes."""
    frame = ninjutso.build_command(ninjutso.CMD_BATTERY, ninjutso.ARG_BATTERY)
    assert frame == bytes([5, 21, 0, 0, 1, 0, 0, 4]) + bytes(24)
    assert len(frame) == ninjutso.PAYLOAD_LEN + 1


def test_paired_pid_command():
    frame = ninjutso.build_command(ninjutso.CMD_PAIRED_PID, ninjutso.ARG_PAIRED_PID)
    assert frame[:8] == bytes([5, 40, 0, 0, 1, 0, 0, 2])


# --------------------------------------------------------------------------
# Battery parse
# --------------------------------------------------------------------------


def test_captured_battery_reply():
    reading = ninjutso.parse_battery(BATTERY_85)
    assert reading.online
    assert reading.percent == 85
    assert reading.charging is False
    assert reading.connection == "2.4 GHz"


def test_charging_flag():
    assert ninjutso.parse_battery(_with(BATTERY_85, _10=1)).charging is True


def test_wired_is_shown_charging_like_the_vendor_does():
    reading = ninjutso.parse_battery(BATTERY_85, wired=True)
    assert reading.charging is True
    assert reading.connection == "wired"


def test_asleep_mouse_is_offline_not_a_reading():
    """[12] is the vendor's wake check. The receiver still answers with the
    mouse off, so the reply existing proves nothing."""
    reading = ninjutso.parse_battery(_with(BATTERY_85, _12=0))
    assert reading is not None
    assert not reading.online
    assert reading.percent is None


def test_reply_to_another_command_is_rejected():
    """A firmware reply has 0x08 at [9]; taken as battery it would read 8%."""
    assert ninjutso.parse_battery(FIRMWARE_REPLY) is None
    assert ninjutso.parse_battery(PAIRED_AE11) is None


def test_argument_echo_is_checked():
    assert ninjutso.parse_battery(_with(BATTERY_85, _7=0)) is None


def test_level_above_100_is_rejected():
    assert ninjutso.parse_battery(_with(BATTERY_85, _9=0xC8)) is None


def test_short_reply_is_rejected():
    assert ninjutso.parse_battery(BATTERY_85[:10]) is None


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_paired_pid_is_little_endian():
    assert ninjutso.parse_paired_pid(PAIRED_AE11) == 0xAE11
    assert ninjutso.parse_paired_pid(BATTERY_85) is None


def test_unpaired_receiver_has_no_identity():
    assert ninjutso.parse_paired_pid(_with(PAIRED_AE11, _9=0, _10=0)) is None


def test_wireless_and_wired_resolve_to_one_key(monkeypatch):
    monkeypatch.setattr(ninjutso, "_paired", {})
    driver = ninjutso.NinjutsoDriver()
    paired = ninjutso.parse_paired_pid(PAIRED_AE11)
    assert driver.identity(_info(0xAE11)) == ninjutso.identity_for(paired)
    assert driver.identity(_info(0xAE1C)) is None


# --------------------------------------------------------------------------
# Collection selection
# --------------------------------------------------------------------------


def test_candidates_pick_the_vendor_collection_only():
    infos = [
        _info(0xAE1C, usage_page=0x0001, interface_number=0),
        _info(0xAE1C, usage_page=0x0001),
        _info(0xAE1C, usage_page=0x000C),
        _info(0xAE1C),
    ]
    chosen = ninjutso.NinjutsoDriver().candidates(infos)
    assert chosen == [infos[3]]


def test_other_nordic_devices_are_left_alone():
    """Orbitalworks and the Starlight-12 share vid 1915."""
    infos = [_info(0x0746, usage_page=0xFF0A), _info(0xF6B0, usage_page=0xFFA0)]
    assert ninjutso.NinjutsoDriver().candidates(infos) == []


def test_read_carries_the_paired_identity(monkeypatch):
    replies = {
        ninjutso.CMD_PAIRED_PID: PAIRED_AE11,
        ninjutso.CMD_BATTERY: BATTERY_85,
    }

    class FakeDevice:
        def send_feature_report(self, data):
            self.last = data[1]

        def get_feature_report(self, report_id, length):
            return list(replies[self.last])

    from contextlib import contextmanager

    @contextmanager
    def fake_open(path):
        yield FakeDevice()

    monkeypatch.setattr(ninjutso.hidio, "open_path", fake_open)
    monkeypatch.setattr(ninjutso, "REPLY_DELAY", 0)
    monkeypatch.setattr(ninjutso, "_paired", {})
    driver = ninjutso.NinjutsoDriver()
    reading = driver.read(_info(0xAE1C))
    assert reading.percent == 85
    assert reading.device_id == "ninjutso:ae11"

    # The pairing query going unanswered later must not split the mouse back
    # out under the receiver's USB key -- that is how `1915:ae1c` appeared.
    replies[ninjutso.CMD_PAIRED_PID] = FIRMWARE_REPLY
    assert driver.read(_info(0xAE1C)).device_id == "ninjutso:ae11"
    assert driver.identity(_info(0xAE1C)) == "ninjutso:ae11"
    key = ninjutso.device_key(_info(0xAE1C))
    assert ninjutso.legacy_aliases() == {key: "ninjutso:ae11"}
