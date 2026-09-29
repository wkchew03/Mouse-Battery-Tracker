"""Wiring: HID discovery -> drivers -> tray.

Kept separate from tray.py so the read path can be exercised from the CLI
(`python -m mbt read`) without constructing any UI.
"""

from __future__ import annotations

import time

import sys

from . import hidio
from .drivers import all_drivers
from .drivers.base import (
    OFFLINE,
    DeviceInfo,
    Driver,
    Reading,
    describe_device,  # noqa: F401  (re-exported)
    device_key,
)


def discover() -> list[tuple[Driver, DeviceInfo]]:
    """Pair each reachable device with the driver that handles it.

    At most one collection per physical device: the first driver to claim a
    device key wins, so a device matched by a specific vendor driver is never
    also polled by a generic fallback.
    """
    infos = hidio.enumerate_devices()
    found: list[tuple[Driver, DeviceInfo]] = []
    claimed: set[str] = set()
    for driver in all_drivers():
        try:
            candidates = driver.candidates(infos)
        except Exception:
            continue
        for info in candidates:
            key = device_key(info)
            if key in claimed:
                continue
            claimed.add(key)
            found.append((driver, info))
    return found


def read_all() -> list[tuple[str, str, Reading]]:
    """Poll every discovered device. Never raises.

    Keyed by physical-mouse identity, so a mouse that can run wired or wireless
    reports under one entry instead of one per USB product id.
    """
    # A fresh poller has no backoff, so every device is read.
    from .poller import Poller

    return Poller().poll()


def legacy_aliases() -> dict[str, str]:
    """Every driver's old-key -> merged-identity map, combined."""
    aliases: dict[str, str] = {}
    for driver in all_drivers():
        module = sys.modules.get(type(driver).__module__)
        getter = getattr(module, "legacy_aliases", None)
        if getter is None:
            continue
        try:
            aliases.update(getter())
        except Exception:
            continue
    # Last, so a merge the user wrote by hand wins over a driver's.
    from .store import user_merges

    aliases.update(user_merges())
    return aliases


_mock_calls = 0


def mock_provider() -> list[tuple[str, str, Reading]]:
    """Fake devices for validating the UI without hardware.

    Deliberately covers the awkward cases: a charging mouse, a low battery, a
    bucket-only reading (older Logitech), and a mouse that starts out on and
    then powers off -- which is what moves it into "Recently used".
    """
    global _mock_calls
    _mock_calls += 1
    jitter = int(time.time() / 5) % 7

    # Report the Pulsar as on for the first poll only, so the demo shows a
    # device transitioning from Connected to Recently used.
    pulsar = Reading(online=True, percent=52) if _mock_calls == 1 else OFFLINE

    return [
        ("372e:1014", "IPI Float 88", Reading(online=True, percent=68 - jitter)),
        (
            "1532:007b",
            "Razer Viper V2 Pro",
            Reading(online=True, percent=14, charging=False),
        ),
        (
            "046d:c547",
            "Logitech G Pro X",
            Reading(online=True, bucket="good", charging=True),
        ),
        ("3554:f508", "Pulsar X2", pulsar),
    ]


def run_tray(mock: bool = False, interval: float = 60.0) -> int:
    from . import dpi, singleton
    from .hud import Hud
    from .poller import Poller
    from .store import Store
    from .tray import TrayApp

    if not singleton.acquire():
        print("Mouse Battery Tracker is already running (check the tray).")
        return 0

    # Must happen before pystray or Tk creates any window, otherwise Windows
    # renders at 96 DPI and upscales the result.
    dpi.enable()

    poller = None
    if mock:
        provider = mock_provider
    else:
        # Backoff lives in the poller, not the tray loop, so a sleeping mouse
        # gets retried less often while a live one still updates every cycle.
        poller = Poller(base_interval=interval)
        provider = poller.poll
    store = Store()
    hud = Hud(store)
    app = TrayApp(store, provider, poll_interval=interval, hud=hud)
    app.poller = poller
    # Opening the HUD wakes the poll loop, so the window shows a reading taken
    # just now rather than up to `interval` seconds ago.
    hud.on_refresh = app.request_refresh
    hud.run_on_poll_thread = app.run_on_poll_thread
    if mock:
        print("Running tray with mock data. Right-click the tray icon.")
    app.run()
    return 0


def print_readings() -> int:
    """`python -m mbt read` - one-shot battery for every detected mouse."""
    results = read_all()
    if not results:
        print("No mice matched by any driver.")
        print("Run 'python -m mbt probe --only-known' to see what is connected.")
        return 1
    width = max(len(label) for _, label, _ in results)
    for key, label, reading in results:
        print(f"{label:<{width}}  {reading.describe():<20}  [{key}]")
    return 0
