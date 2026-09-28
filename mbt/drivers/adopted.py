"""Mice the user added from the HUD's "Add mouse" scan.

Two kinds, one list. An *adopted* mouse is read with a known driver's protocol
after the user confirmed it; a *placeholder* (driver None) is one nothing can
read, kept so the mouse still appears -- present, nameable, "no battery data".

Many brands are rebadges of a handful of platforms, so a mouse no driver
claims often speaks a protocol one of them already knows, under a vid/pid that
driver was never told about. The HUD's "Try known protocols" sends each
driver's battery query to such a mouse (`try_protocols`), shows every answer,
and the user keeps one only after checking it against the vendor's software.
That confirmation is the check here: a guessed protocol answering with a
plausible wrong number is the failure this project has shipped twice.

How a driver is pointed at a device it would not claim: the device's
collections are handed to it *disguised* (`disguise`) as the model it was
written for -- that model's vid, and its pid where the driver filters or
branches on pid. The serial is replaced with one derived from the real device,
so anything a driver caches by device key (Ninjutso's pairing, Pulsar's
address, Logitech's unit id) is namespaced away from any real mouse of that
model. Results are reported under the real device's key, with `device_id` and
`connection` dropped: both would describe the model it was disguised as.

A placeholder never opens the device, so like zowie.py **its `online` means
enumerated, not awake**: a receiver left plugged in with its mouse switched off
still reads as connected.

Kept in adopted.json as [vid, pid, driver name, as_vid, as_pid], the last three
null for a placeholder; one entry per device, so adopting a placeholder
replaces it and keeps its history and name (same device key). Registered after
every real driver, so one that later supports the mouse properly wins.

STATUS: mechanism only; no user-added mouse has been read on real hardware yet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from .base import DeviceInfo, Reading, device_key, register

FILENAME = "adopted.json"

NO_BATTERY = "no battery data"
PRESENT = Reading(online=True, bucket=NO_BATTERY)


class Profile(NamedTuple):
    label: str
    driver: str
    vendor_id: int
    # None keeps the device's own pid: for drivers that claim by vid alone and
    # never branch on pid.
    product_id: int | None


# One entry per distinct protocol variant, not per model: a driver whose pid
# picks a framing (Orbitalworks' receiver flag and protocol) gets one per
# framing. ZOWIE has no protocol to try.
PROFILES = (
    Profile("Logitech HID++", "logitech", 0x046D, None),
    Profile("Razer", "razer", 0x1532, None),
    Profile("Pulsar / Hitscan", "pulsar", 0x3710, None),
    Profile("IPI / PIAO", "ipi", 0x372E, 0x1014),
    Profile("Orbitalworks (DMS, receiver)", "orbital", 0x1915, 0x0746),
    Profile("Orbitalworks (DMS, wired)", "orbital", 0x1915, 0x0747),
    Profile("Orbitalworks (DMS v2, receiver)", "orbital", 0x1915, 0x080B),
    Profile("Orbitalworks (DMS v2, wired)", "orbital", 0x1915, 0x080C),
    Profile("VAXEE", "vaxee", 0x3057, 0x1001),
    Profile("CompX (LAMZU, Attack Shark, G-Wolves)", "compx-gen2", 0x373E, None),
    Profile("Ninjutso", "ninjutso", 0x1915, 0xAE1C),
    Profile("Finalmouse", "finalmouse", 0x361D, 0x0100),
)


class Trial(NamedTuple):
    profile: Profile
    reading: Reading


class Adoption(NamedTuple):
    vendor_id: int
    product_id: int
    # None, and the two below with it, for a placeholder.
    driver: str | None = None
    as_vendor_id: int | None = None
    as_product_id: int | None = None


def _int(value):
    return None if value is None else int(value)


def default_path() -> Path:
    # Imported here: store imports drivers.base, so a module-level import
    # would be circular.
    from ..store import app_dir

    return app_dir() / FILENAME


def load(path: Path | None = None) -> list[Adoption]:
    """The mice the user added. Missing or damaged file: none."""
    try:
        raw = json.loads((path or default_path()).read_text(encoding="utf-8"))
        return [Adoption(int(v), int(p), None if d is None else str(d), _int(av), _int(ap))
                for v, p, d, av, ap in raw]
    except (OSError, ValueError, TypeError):
        return []


def add(adoption: Adoption, path: Path | None = None) -> None:
    """Record an adoption, replacing any earlier one for the same device."""
    from ..store import _write_json

    kept = [a for a in load(path)
            if (a.vendor_id, a.product_id) != (adoption.vendor_id, adoption.product_id)]
    _write_json(path or default_path(), [list(a) for a in kept + [adoption]])


def is_mouse_collection(info: DeviceInfo) -> bool:
    return info.get("usage_page") == 0x0001 and info.get("usage") == 0x02


def disguise(info: DeviceInfo, vendor_id: int, product_id: int | None) -> DeviceInfo:
    """`info` as the model a driver was written for. The path is untouched."""
    return {
        **info,
        "vendor_id": vendor_id,
        "product_id": info.get("product_id") if product_id is None else product_id,
        "serial_number": f"adopted:{device_key(info)}",
    }


def is_answer(reading: Reading) -> bool:
    """A reading worth showing the user: the mouse replied with a level."""
    if not reading.online:
        return False
    if reading.percent is not None:
        return 0 <= reading.percent <= 100
    return reading.millivolts is not None or reading.bucket is not None


def _clean(reading: Reading) -> Reading:
    return replace(reading, device_id=None, connection=None)


def _drivers_by_name(drivers) -> dict:
    if drivers is None:
        from .base import all_drivers

        drivers = all_drivers()
    return {d.name: d for d in drivers}


def try_protocols(infos: list[DeviceInfo], drivers=None) -> list[Trial]:
    """Every profile that got a level out of this device's collections.

    `infos` is one device's collections. Sends each driver's queries to it, so
    it opens the device: poll thread only, and only when the user asked.
    """
    by_name = _drivers_by_name(drivers)
    trials = []
    for profile in PROFILES:
        driver = by_name.get(profile.driver)
        if driver is None:
            continue
        disguised = [disguise(i, profile.vendor_id, profile.product_id) for i in infos]
        try:
            chosen = driver.candidates(disguised)
            if not chosen:
                continue
            reading = driver.read(chosen[0])
        except Exception:
            continue
        if is_answer(reading):
            trials.append(Trial(profile, _clean(reading)))
    return trials


class AdoptedDriver:
    name = "adopted"

    def __init__(self, path: Path | None = None, drivers=None) -> None:
        self.path = path
        self._drivers = drivers
        # Real path -> (driver or None, adoption), from the last candidates().
        self._routes: dict[bytes, tuple] = {}

    def candidates(self, infos: list[DeviceInfo]) -> list[DeviceInfo]:
        adoptions = load(self.path)
        self._routes = {}
        if not adoptions:
            return []
        by_name = _drivers_by_name(self._drivers)
        chosen = []
        for adoption in adoptions:
            real = {
                info["path"]: info for info in infos
                if (info.get("vendor_id"), info.get("product_id"))
                == (adoption.vendor_id, adoption.product_id)
            }
            driver = by_name.get(adoption.driver)
            if adoption.driver is None:
                # A placeholder claims the mouse collection, which it never opens.
                picked = [i for i in real.values() if is_mouse_collection(i)]
            elif driver is None or not real:
                continue
            else:
                disguised = [disguise(i, adoption.as_vendor_id, adoption.as_product_id)
                             for i in real.values()]
                try:
                    picked = driver.candidates(disguised)
                except Exception:
                    continue
            for info in picked:
                self._routes[info["path"]] = (driver, adoption)
                chosen.append(real[info["path"]])
        return chosen

    def read(self, info: DeviceInfo) -> Reading:
        driver, adoption = self._routes[info["path"]]
        if driver is None:
            return PRESENT  # never opens the device
        disguised = disguise(info, adoption.as_vendor_id, adoption.as_product_id)
        return _clean(driver.read(disguised))


register(AdoptedDriver())
