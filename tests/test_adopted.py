"""Mice added from "Add mouse": adopted into a known driver, or placeholders.

What matters: the driver sees the model it was written for, nothing it caches
can collide with a real mouse of that model, results land under the unknown
mouse's own key, only a real level is ever offered to the user, and a
placeholder never opens the device or invents a level.
"""

from mbt import probe
from mbt.drivers import adopted
from mbt.drivers.base import OFFLINE, Reading, device_key

UNKNOWN_VID, UNKNOWN_PID = 0x05AC, 0x024F


def _info(usage_page, usage, serial="000000000001"):
    return {
        "vendor_id": UNKNOWN_VID,
        "product_id": UNKNOWN_PID,
        "usage_page": usage_page,
        "usage": usage,
        "interface_number": 0,
        "path": f"unknown-{usage_page:04x}".encode(),
        "serial_number": serial,
        "manufacturer_string": "SINO WEALTH",
        "product_string": "Gaming Mouse",
    }


MOUSE = _info(0x0001, 0x02)
VENDOR = _info(0xFF00, 0x01)


class FakeDriver:
    """Claims the vendor collection of its own vid/pid, like a real driver."""

    def __init__(self, name, vid, pid, reading):
        self.name, self.vid, self.pid, self.reading = name, vid, pid, reading
        self.seen = []

    def candidates(self, infos):
        return [i for i in infos
                if (i["vendor_id"], i["product_id"]) == (self.vid, self.pid)
                and i["usage_page"] >= 0xFF00]

    def read(self, info):
        self.seen.append(info)
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


def test_disguise_keeps_the_path_and_namespaces_the_serial():
    disguised = adopted.disguise(VENDOR, 0x3057, 0x1001)
    assert disguised["path"] == VENDOR["path"]
    assert (disguised["vendor_id"], disguised["product_id"]) == (0x3057, 0x1001)
    # A placeholder serial would otherwise give the disguised device the exact
    # key of a real VAXEE, and share its caches.
    assert device_key(disguised) != "3057:1001"
    assert device_key(MOUSE) in device_key(disguised)


def test_disguise_can_keep_the_devices_own_pid():
    disguised = adopted.disguise(VENDOR, 0x1532, None)
    assert disguised["product_id"] == UNKNOWN_PID


def test_trials_report_only_profiles_that_gave_a_level():
    answers = FakeDriver("vaxee", 0x3057, 0x1001,
                         Reading(online=True, percent=64, device_id="x", connection="wired"))
    silent = FakeDriver("finalmouse", 0x361D, 0x0100, OFFLINE)
    broken = FakeDriver("ninjutso", 0x1915, 0xAE1C, OSError("no reply"))
    nonsense = FakeDriver("ipi", 0x372E, 0x1014, Reading(online=True, percent=212))

    trials = adopted.try_protocols([MOUSE, VENDOR], [answers, silent, broken, nonsense])

    assert [t.profile.driver for t in trials] == ["vaxee"]
    # Both describe the model it was disguised as, not this mouse.
    assert trials[0].reading == Reading(online=True, percent=64)
    assert answers.seen[0]["path"] == VENDOR["path"]


def test_an_adopted_mouse_is_read_under_its_own_key(tmp_path):
    path = tmp_path / "adopted.json"
    inner = FakeDriver("vaxee", 0x3057, 0x1001,
                       Reading(online=True, percent=64, device_id="vaxee:x"))
    driver = adopted.AdoptedDriver(path, drivers=[inner])
    assert driver.candidates([MOUSE, VENDOR]) == []

    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID, "vaxee", 0x3057, 0x1001), path)
    chosen = driver.candidates([MOUSE, VENDOR])

    # The real collection comes back, so the poller keys it as this mouse.
    assert chosen == [VENDOR]
    assert driver.read(VENDOR) == Reading(online=True, percent=64)
    assert inner.seen[-1]["vendor_id"] == 0x3057


def test_adopting_again_replaces_the_earlier_choice(tmp_path):
    path = tmp_path / "adopted.json"
    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID, "vaxee", 0x3057, 0x1001), path)
    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID, "razer", 0x1532, None), path)
    assert adopted.load(path) == [
        adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID, "razer", 0x1532, None)
    ]


def test_a_damaged_file_means_no_adoptions(tmp_path):
    path = tmp_path / "adopted.json"
    path.write_text('[[1, 2]]', encoding="utf-8")
    assert adopted.load(path) == []
    path.write_text("{not json", encoding="utf-8")
    assert adopted.load(path) == []


def test_every_profile_names_a_registered_driver():
    from mbt.drivers import all_drivers

    names = [d.name for d in all_drivers()]
    assert {p.driver for p in adopted.PROFILES} <= set(names)
    # After every real driver, so one that supports the mouse properly wins.
    assert names[-1] == "adopted"


def test_a_placeholder_claims_only_the_mouse_collection(tmp_path, monkeypatch):
    path = tmp_path / "adopted.json"
    driver = adopted.AdoptedDriver(path, drivers=[])
    other = dict(MOUSE, product_id=0x9999, path=b"other")
    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID), path)
    assert adopted.load(path) == [adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID)]

    assert driver.candidates([MOUSE, VENDOR, other]) == [MOUSE]

    def refuse(*_args, **_kwargs):
        raise AssertionError("a placeholder must never open the device")

    monkeypatch.setattr("mbt.hidio.open_path", refuse)
    reading = driver.read(MOUSE)
    assert reading == Reading(online=True, bucket=adopted.NO_BATTERY)


def test_adopting_a_placeholder_replaces_it(tmp_path):
    path = tmp_path / "adopted.json"
    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID), path)
    adopted.add(adopted.Adoption(UNKNOWN_VID, UNKNOWN_PID, "vaxee", 0x3057, 0x1001), path)
    assert [a.driver for a in adopted.load(path)] == ["vaxee"]


def test_scan_offers_unclaimed_mice_only():
    """A claimed device, and a device with no mouse collection, are not offered."""
    claimed = dict(MOUSE, vendor_id=0x1532, product_id=0x0001, serial_number="R1",
                   path=b"razer")
    keypad = dict(MOUSE, vendor_id=0x2222, product_id=0x0001, usage=0x06,
                  serial_number="K1", path=b"keypad")
    groups = probe.unknown_mice([MOUSE, VENDOR, claimed, keypad], {device_key(claimed)})
    assert groups == [[MOUSE, VENDOR]]


def test_diagnosis_names_a_supported_vendor():
    assert probe.diagnose([dict(MOUSE, vendor_id=0x1532)]) == (
        "Razer is supported, but not this model yet.")
    assert "new driver" in probe.diagnose([MOUSE, VENDOR])
    assert "no channel" in probe.diagnose([MOUSE])
