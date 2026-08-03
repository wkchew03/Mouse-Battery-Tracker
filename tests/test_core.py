"""Tests for identity, persistence, formatting and menu construction.

No hardware required. Protocol parsers get their own fixture-based tests as
each driver lands.
"""

from pathlib import Path

import pytest

from mbt.app import mock_provider
from mbt.drivers.base import Reading, device_key, is_placeholder_serial
from mbt.store import DeviceRecord, Store, format_age
from mbt.theme import dim
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
        (86400 * 14, "2w ago"),
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


def test_dim_darkens_every_channel():
    """Disconnected mice were rendering in the same full-brightness colour as
    the connected one, so the two were indistinguishable."""
    bright = level_color(90)
    dimmed = dim(bright)
    assert dimmed != bright
    for channel in range(3):
        assert dimmed[channel] < bright[channel]


def test_dim_preserves_hue_so_the_level_still_reads():
    """A stale 45% should still look amber, not grey."""
    amber = dim(level_color(45))
    green = dim(level_color(90))
    assert amber != green
    # Amber stays red-dominant, green stays green-dominant.
    assert amber[0] > amber[2]
    assert green[1] > green[0]


def test_dim_keeps_alpha():
    assert dim(level_color(50))[3] == level_color(50)[3]


def test_dim_stays_visible_against_the_card():
    """Too dark and the bar disappears into the card background."""
    card = (42, 44, 51)
    for percent in (10, 50, 100):
        dimmed = dim(level_color(percent))
        assert sum(dimmed[:3]) > sum(card) + 30


@pytest.mark.parametrize("percent", [None, 5, 68, 100])
def test_render_icon(percent):
    image = render_icon(percent)
    assert image.size == (64, 64)
    assert image.mode == "RGBA"
