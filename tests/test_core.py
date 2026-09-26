"""Tests for identity, persistence, formatting and menu construction.

No hardware required. Protocol parsers get their own fixture-based tests as
each driver lands.
"""

from pathlib import Path

import time

import pytest

from mbt.app import mock_provider
from mbt.drivers.base import Reading, device_key, is_placeholder_serial
from mbt.store import (
    MAX_ALERT_THRESHOLD,
    MIN_ALERT_THRESHOLD,
    DeviceRecord,
    Store,
    format_age,
)
from mbt.tray import TrayApp, level_color, render_icon


@pytest.mark.parametrize(
    "serial,expected",
    [
        ("000000000001", True),  # the IPI Float 88 dongle's constant serial
        ("0000", True),
        ("0", True),
        ("", True),
        ("A00WA4221PPVD6", False),
        ("0000123456", False),
    ],
)
def test_is_placeholder_serial(serial, expected):
    assert is_placeholder_serial(serial) is expected


@pytest.mark.parametrize(
    "info,expected",
    [
        # A placeholder serial must not become part of the identity, or two
        # different mice of the same model would collide onto one record.
        ({"vendor_id": 0x372E, "product_id": 0x1014, "serial_number": "000000000001"},
         "372e:1014"),
        ({"vendor_id": 0x0FD9, "product_id": 0x0084, "serial_number": "A00WA4221PPVD6"},
         "0fd9:0084:A00WA4221PPVD6"),
        ({"vendor_id": 0x1532, "product_id": 0x007B, "serial_number": ""}, "1532:007b"),
    ],
)
def test_device_key(info, expected):
    assert device_key(info) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (10, "just now"),
        (300, "5m ago"),
        (3600 * 3, "3h ago"),
        (86400 * 2, "2d ago"),
        (86400 * 14, "14d ago"),
        # Past a week still counts in days -- the old code said "3w ago" here.
        (86400 * 23.7, "23d ago"),
        (86400 * 400, "400d ago"),
    ],
)
def test_format_age(seconds, expected):
    assert format_age(seconds) == expected


def test_reading_describe():
    assert Reading(online=True, percent=72).describe() == "72%"
    assert Reading(online=True, percent=72, charging=True).describe() == "72% (charging)"
    assert Reading(online=True, bucket="good").describe() == "good"
    assert Reading(online=True).describe() == "unknown"
    assert Reading(online=False, percent=72).describe() == "off"


def test_offline_read_preserves_last_known_percent(tmp_path: Path):
    store = Store(tmp_path)
    store.load()
    store.update("1532:007b", "Viper", Reading(online=True, percent=41))
    store.update("1532:007b", "Viper", Reading(online=False))
    assert store.records["1532:007b"].percent == 41


def test_store_round_trip(tmp_path: Path):
    store = Store(tmp_path)
    store.load()
    store.update("372e:1014", "IPI Float 88", Reading(online=True, percent=72))
    store.save()

    reloaded = Store(tmp_path)
    reloaded.load()
    assert reloaded.records["372e:1014"].percent == 72
    assert reloaded.records["372e:1014"].label == "IPI Float 88"


def test_display_name_overrides_device_string(tmp_path: Path):
    store = Store(tmp_path)
    store.load()
    store.set_display_name("372e:1014", "Float 88")
    assert store.display_name("372e:1014", "PIAO-2.4G") == "Float 88"
    assert store.display_name("unknown-key", "PIAO-2.4G") == "PIAO-2.4G"


def test_record_shows_last_known_level_not_off():
    """Regression: the Recent list must show the stored percentage.

    Routing this through Reading.describe() returns "off" for a stored record
    (which always has online=False) and hides the number entirely.
    """
    record = DeviceRecord(key="3554:f508", label="Pulsar X2", percent=52)
    assert record.describe_last_known() == "52%"
    assert DeviceRecord(key="x", bucket="good").describe_last_known() == "good"
    assert DeviceRecord(key="x").describe_last_known() == "unknown"


def test_menu_moves_powered_off_mouse_to_recent(tmp_path: Path):
    store = Store(tmp_path)
    store.load()
    app = TrayApp(store, mock_provider, poll_interval=999)
    app.poll_once()  # mock reports the Pulsar as on
    app.poll_once()  # and then off

    text = "\n".join(str(item.text) for item in app._menu_items())
    assert "Connected" in text
    assert "Recently used" in text
    # The powered-off mouse keeps its last known level rather than showing "off".
    assert "Pulsar X2 — 52%" in text
    assert "Quit" in text


def test_level_color_thresholds():
    assert level_color(80) != level_color(30)
    assert level_color(30) != level_color(10)
    assert level_color(None) == level_color(None)
    # Charging wins over level so a charging mouse never looks like a warning.
    assert level_color(5, charging=True) != level_color(5)


@pytest.mark.parametrize("percent", [None, 5, 68, 100])
def test_render_icon(percent):
    image = render_icon(percent)
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


# --------------------------------------------------------------------------
# Absolute timestamps and the settings file
# --------------------------------------------------------------------------


def test_settings_default_when_the_file_is_absent(tmp_path):
    store = Store(directory=tmp_path)
    store.load()
    assert store.alert_threshold == 15
    assert store.notify_low is True
    assert store.alert_clear == 25


def test_settings_round_trip(tmp_path):
    store = Store(directory=tmp_path)
    store.load()
    store.set_alert_threshold(35)
    store.set_notify_low(False)

    reloaded = Store(directory=tmp_path)
    reloaded.load()
    assert reloaded.alert_threshold == 35
    assert reloaded.notify_low is False


def test_threshold_is_clamped_to_something_useful(tmp_path):
    store = Store(directory=tmp_path)
    store.load()
    assert store.set_alert_threshold(0) == MIN_ALERT_THRESHOLD
    assert store.set_alert_threshold(999) == MAX_ALERT_THRESHOLD


def test_a_corrupt_settings_file_falls_back_per_field(tmp_path):
    """A bad value must not disable the alert -- silence is the one failure
    mode nobody notices until the mouse is flat."""
    (tmp_path / "settings.json").write_text(
        '{"alert_threshold": "twenty", "notify_low": true}', encoding="utf-8"
    )
    store = Store(directory=tmp_path)
    store.load()
    assert store.alert_threshold == 15
    assert store.notify_low is True


# --------------------------------------------------------------------------
# merge_aliases -- folding a device's old identity key into a new one
# --------------------------------------------------------------------------


def test_merge_aliases_moves_an_orphaned_record_to_the_new_key(tmp_path):
    store = Store(directory=tmp_path)
    store.update("old:1", "Mouse", Reading(online=True, percent=50))

    merged = store.merge_aliases({"old:1": "new:1"})
    assert merged == 1
    assert "old:1" not in store.records
    assert store.records["new:1"].percent == 50


def test_merge_aliases_combines_history_instead_of_discarding_the_loser(tmp_path):
    """The real-world case: a mouse migrates to a new identity key (Logitech's
    unit id, once read) while it already has months of drain-rate samples
    under the old one. Picking "whichever record is newer" and throwing the
    other away would silently erase that history."""
    store = Store(directory=tmp_path)

    old_ts = 1_000_000.0
    new_ts = 2_000_000.0
    store.records["old:1"] = DeviceRecord(
        key="old:1",
        label="Logitech G PRO X SUPERLIGHT 2",
        percent=79,
        last_online=old_ts,
        history=[[old_ts - 3600, 80], [old_ts, 79]],
    )
    store.records["new:1"] = DeviceRecord(
        key="new:1",
        label="Logitech USB Receiver",
        percent=80,
        last_online=new_ts,
        history=[[new_ts, 80]],
    )

    store.merge_aliases({"old:1": "new:1"})

    assert "old:1" not in store.records
    survivor = store.records["new:1"]
    # The live fields come from whichever side actually answered more
    # recently -- here, the new key's own reading.
    assert survivor.percent == 80
    assert survivor.last_online == new_ts
    # But nothing from the losing side's timeline is dropped.
    assert survivor.history == [[old_ts - 3600, 80], [old_ts, 79], [new_ts, 80]]


def test_merge_aliases_folds_the_display_name_onto_the_new_key(tmp_path):
    store = Store(directory=tmp_path)
    store.update("old:1", "Logitech USB Receiver", Reading(online=True, percent=50))
    store.update("new:1", "Logitech USB Receiver", Reading(online=True, percent=51))
    store.set_display_name("old:1", "Logitech G PRO X SUPERLIGHT 2")

    store.merge_aliases({"old:1": "new:1"})

    assert "old:1" not in store.names
    assert store.display_name("new:1") == "Logitech G PRO X SUPERLIGHT 2"


def test_merge_aliases_does_nothing_when_the_old_key_is_absent(tmp_path):
    store = Store(directory=tmp_path)
    store.update("new:1", "Mouse", Reading(online=True, percent=50))
    assert store.merge_aliases({"old:1": "new:1"}) == 0
    assert store.records["new:1"].percent == 50
