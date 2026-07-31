"""Merged device identity, image naming, and legacy-key migration."""

import time

from mbt.drivers import ipi
from mbt.drivers.base import (
    Reading,
    collapse_duplicates,
    reading_rank,
    resolve_identity,
    resolve_label,
)
from mbt.mouseart import candidate_paths, custom_image_path, safe_filename
from mbt.store import DeviceRecord, Store


def _info(pid):
    return {
        "vendor_id": ipi.VENDOR_IPI,
        "product_id": pid,
        "usage_page": 0xFF00,
        "usage": 0x01,
        "interface_number": 2,
        "path": b"test",
        "serial_number": "000000000001",
        "product_string": "PIAO-2.4G",
        "manufacturer_string": "Gaming Mouse 8K",
    }


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_wired_and_wireless_share_one_identity():
    driver = ipi.IpiDriver()
    assert resolve_identity(driver, _info(0x1014)) == resolve_identity(
        driver, _info(0x1015)
    )


def test_label_is_stable_across_connection_types():
    """Without this the entry renames itself when you plug the cable in."""
    driver = ipi.IpiDriver()
    assert resolve_label(driver, _info(0x1014)) == "IPI Float 88"
    assert resolve_label(driver, _info(0x1015)) == "IPI Float 88"


def test_unverified_models_are_not_merged():
    """Only ids confirmed to be one mouse may share an identity."""
    driver = ipi.IpiDriver()
    assert resolve_identity(driver, _info(0x1028)) != resolve_identity(
        driver, _info(0x1014)
    )


def test_identity_falls_back_to_hardware_key():
    class Bare:
        name = "bare"
        vendor_ids = frozenset()

    info = dict(_info(0x1014), vendor_id=0x1532, product_id=0x007B)
    assert resolve_identity(Bare(), info) == "1532:007b"


def test_driver_errors_do_not_break_identity():
    class Broken:
        name = "broken"
        vendor_ids = frozenset()

        def identity(self, info):
            raise RuntimeError("boom")

        def label(self, info):
            raise RuntimeError("boom")

    info = _info(0x1014)
    assert resolve_identity(Broken(), info) == "372e:1014"
    assert resolve_label(Broken(), info)  # falls back to the USB strings


# --------------------------------------------------------------------------
# Duplicate collapsing
# --------------------------------------------------------------------------


def test_online_reading_wins_over_offline_duplicate():
    """Dongle left in while running wired must not hide the live reading."""
    assert reading_rank(Reading(online=True, percent=80)) > reading_rank(
        Reading(online=True)
    )
    assert reading_rank(Reading(online=True)) > reading_rank(Reading(online=False))


def test_collapse_keeps_the_live_entry():
    results = [
        ("ipi:float88", "IPI Float 88", Reading(online=False)),
        ("ipi:float88", "IPI Float 88", Reading(online=True, percent=80)),
    ]
    collapsed = collapse_duplicates(results)
    assert len(collapsed) == 1
    assert collapsed[0][2].percent == 80


def test_collapse_is_order_independent():
    """Whichever order the two forms enumerate in, the live one must win."""
    live = ("ipi:float88", "IPI Float 88", Reading(online=True, percent=80))
    dead = ("ipi:float88", "IPI Float 88", Reading(online=False))
    assert collapse_duplicates([live, dead])[0][2].percent == 80
    assert collapse_duplicates([dead, live])[0][2].percent == 80


def test_collapse_keeps_distinct_mice_separate():
    results = [
        ("ipi:float88", "IPI Float 88", Reading(online=True, percent=80)),
        ("1532:007b", "Razer", Reading(online=True, percent=40)),
    ]
    assert len(collapse_duplicates(results)) == 2


# --------------------------------------------------------------------------
# Image naming
# --------------------------------------------------------------------------


def test_image_named_after_the_mouse(tmp_path):
    assert custom_image_path(tmp_path, "ipi:float88", "IPI Float 88").name == (
        "IPI Float 88.png"
    )


def test_illegal_filename_characters_are_replaced():
    assert ":" not in safe_filename('My: "Mouse"/1')
    assert "/" not in safe_filename('My: "Mouse"/1')


def test_blank_name_falls_back_to_key(tmp_path):
    assert custom_image_path(tmp_path, "ipi:float88", "").name == "ipi_float88.png"


def test_candidates_prefer_name_then_key(tmp_path):
    paths = candidate_paths(tmp_path, "ipi:float88", "Float 88")
    names = [p.name for p in paths]
    assert names[0] == "Float 88.png"
    assert "ipi_float88.png" in names


def test_candidates_accept_common_image_types(tmp_path):
    suffixes = {p.suffix for p in candidate_paths(tmp_path, "k", "Name")}
    assert {".png", ".jpg", ".webp"} <= suffixes


# --------------------------------------------------------------------------
# Migration of history saved under the old per-device keys
# --------------------------------------------------------------------------


def test_legacy_keys_fold_into_merged_identity(tmp_path):
    store = Store(directory=tmp_path)
    store.records = {
        "372e:1014": DeviceRecord(key="372e:1014", percent=80, last_online=time.time())
    }
    store.names = {"372e:1014": "My Mouse"}

    merged = store.merge_aliases(ipi.legacy_aliases())

    assert merged == 1
    assert "372e:1014" not in store.records
    assert store.records["ipi:float88"].percent == 80
    assert store.names["ipi:float88"] == "My Mouse"


def test_migration_keeps_the_newer_record(tmp_path):
    store = Store(directory=tmp_path)
    now = time.time()
    store.records = {
        "372e:1014": DeviceRecord(key="372e:1014", percent=80, last_online=now),
        "ipi:float88": DeviceRecord(key="ipi:float88", percent=40, last_online=now - 500),
    }
    store.merge_aliases(ipi.legacy_aliases())
    assert store.records["ipi:float88"].percent == 80


def test_migration_is_idempotent(tmp_path):
    store = Store(directory=tmp_path)
    store.records = {"372e:1015": DeviceRecord(key="372e:1015", percent=55)}
    store.merge_aliases(ipi.legacy_aliases())
    assert store.merge_aliases(ipi.legacy_aliases()) == 0
    assert store.records["ipi:float88"].percent == 55
