"""The feed published for the Stream Deck plugin."""

import json
import time

from mbt import feed
from mbt.drivers.base import Reading
from mbt.store import DeviceRecord, Store


def _store(tmp_path):
    store = Store(directory=tmp_path)
    store.update("a:1", "Mouse A", Reading(online=True, percent=80))
    store.update("b:2", "Mouse B", Reading(online=True, percent=20))
    return store


def test_payload_lists_every_mouse(tmp_path):
    payload = feed.build_payload(_store(tmp_path), online_keys={"a:1"})
    assert {m["key"] for m in payload["mice"]} == {"a:1", "b:2"}


def test_connected_mice_come_first(tmp_path):
    payload = feed.build_payload(_store(tmp_path), online_keys={"b:2"})
    assert payload["mice"][0]["key"] == "b:2"
    assert payload["mice"][0]["connected"] is True
    assert payload["mice"][1]["connected"] is False


def test_generated_at_lets_a_reader_detect_staleness(tmp_path):
    """Without this a reading from a dead app looks identical to a live one."""
    payload = feed.build_payload(_store(tmp_path))
    assert abs(payload["generated_at"] - time.time()) < 5


def test_disconnected_mouse_keeps_its_last_level(tmp_path):
    payload = feed.build_payload(_store(tmp_path), online_keys=set())
    entry = next(m for m in payload["mice"] if m["key"] == "a:1")
    assert entry["percent"] == 80
    assert entry["connected"] is False
    assert entry["last_seen_text"]


def test_icon_names_are_filename_safe(tmp_path):
    payload = feed.build_payload(_store(tmp_path))
    for entry in payload["mice"]:
        assert ":" not in entry["icon"]
        assert entry["icon"].endswith(".png")


def test_publish_writes_json_and_icons(tmp_path):
    store = _store(tmp_path)
    payload = feed.publish(store, online_keys={"a:1"})

    written = json.loads((feed.feed_dir(store) / "feed.json").read_text("utf-8"))
    assert written["version"] == feed.FEED_VERSION
    assert len(written["mice"]) == 2

    for entry in payload["mice"]:
        icon = feed.feed_dir(store) / "icons" / entry["icon"]
        assert icon.exists() and icon.stat().st_size > 0


def test_publish_leaves_no_temp_files(tmp_path):
    """Writes are atomic; a reader must never see a half-written file."""
    store = _store(tmp_path)
    feed.publish(store, online_keys=set())
    leftovers = list(feed.feed_dir(store).rglob("*.tmp"))
    assert leftovers == []


def test_publish_survives_an_unwritable_directory(tmp_path):
    """A feed failure must never take down the poll loop."""
    store = Store(directory=tmp_path / "nope")
    store.update("a:1", "Mouse A", Reading(online=True, percent=50))
    blocker = tmp_path / "nope"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")
    feed.publish(store)  # must not raise


def test_percent_may_be_null_while_charging(tmp_path):
    """Some firmware reports charging without a level; the feed must not invent one."""
    store = Store(directory=tmp_path)
    store.update("c:3", "Charger", Reading(online=True, charging=True))
    payload = feed.build_payload(store, online_keys={"c:3"})
    entry = payload["mice"][0]
    assert entry["percent"] is None
    assert entry["charging"] is True


def test_a_record_that_never_answered_says_never(tmp_path):
    """A dongle seen only in firmware-update mode leaves a record with no
    last_online. Ageing that from epoch printed "20688d ago" on the Stream
    Deck; the HUD and tray filter these out, but the feed publishes every
    record, so it has to word it."""
    store = Store(tmp_path)
    store.records["373e:b01e"] = DeviceRecord(key="373e:b01e", label="Maya X DFU")

    entry = feed.build_payload(store)["mice"][0]
    assert entry["last_seen_text"] == "never"
    assert entry["percent"] is None
