"""Manual refresh: opening the HUD must produce a reading taken now."""

from unittest.mock import patch

from mbt.drivers.base import OFFLINE, Reading
from mbt.poller import Poller
from mbt.store import Store
from mbt.tray import TrayApp


class FakeIcon:
    def __init__(self, *args, **kwargs):
        self.icon = kwargs.get("icon")
        self.title = kwargs.get("title", "")
        self.menu = kwargs.get("menu")

    def notify(self, *args, **kwargs):
        pass

    def update_menu(self):
        pass

    def stop(self):
        pass


class FakeDriver:
    name = "fake"

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0

    def candidates(self, infos):
        return [{"vendor_id": 0x1234, "product_id": 1, "path": b"p", "serial_number": ""}]

    def read(self, info):
        self.calls += 1
        return self.readings[min(self.calls - 1, len(self.readings) - 1)]


def _poller_with(driver):
    poller = Poller(base_interval=60)
    patcher = patch("mbt.poller.discover", return_value=[(driver, driver.candidates([])[0])])
    patcher.start()
    return poller, patcher


# base_interval 60, failure_threshold 3, max_interval 300. Polling at each due
# time drives the device four failures deep, after which it is not due again
# for 240s -- long enough that a plain refresh would skip it.
_DUE_TIMES = (0.0, 60.0, 120.0, 240.0)
_TOO_SOON = 300.0


def test_backoff_delays_a_retry():
    """Baseline: after repeated failures the device is not re-read."""
    driver = FakeDriver([OFFLINE])
    poller, patcher = _poller_with(driver)
    try:
        for now in _DUE_TIMES:
            poller.poll(now=now)
        before = driver.calls
        poller.poll(now=_TOO_SOON)
        assert driver.calls == before
    finally:
        patcher.stop()


def test_reset_backoff_forces_an_immediate_reread():
    """The bug this fixes: a mouse switched on mid-backoff stayed invisible,
    because refreshing returned the cached 'off' instead of re-reading."""
    driver = FakeDriver(
        [OFFLINE, OFFLINE, OFFLINE, OFFLINE, Reading(online=True, percent=77)]
    )
    poller, patcher = _poller_with(driver)
    try:
        for now in _DUE_TIMES:
            poller.poll(now=now)
        # Without the reset this returns the cached offline reading.
        assert poller.poll(now=_TOO_SOON)[0][2].online is False

        poller.reset_backoff()
        results = poller.poll(now=_TOO_SOON)
        assert results[0][2].percent == 77
    finally:
        patcher.stop()


def test_request_refresh_clears_backoff_and_wakes(tmp_path):
    store = Store(directory=tmp_path)
    with patch("mbt.tray.pystray.Icon", FakeIcon):
        app = TrayApp(store, provider=lambda: [], poll_interval=60)

    cleared = {"value": False}

    class StubPoller:
        def reset_backoff(self):
            cleared["value"] = True

    app.poller = StubPoller()
    app.request_refresh()

    assert cleared["value"] is True
    assert app._wake.is_set() is True


def test_request_refresh_without_a_poller(tmp_path):
    """Mock mode has no Poller; refreshing must still not raise."""
    store = Store(directory=tmp_path)
    with patch("mbt.tray.pystray.Icon", FakeIcon):
        app = TrayApp(store, provider=lambda: [], poll_interval=60)
    app.request_refresh()
    assert app._wake.is_set() is True


def test_broken_poller_does_not_block_refresh(tmp_path):
    store = Store(directory=tmp_path)
    with patch("mbt.tray.pystray.Icon", FakeIcon):
        app = TrayApp(store, provider=lambda: [], poll_interval=60)

    class Broken:
        def reset_backoff(self):
            raise RuntimeError("boom")

    app.poller = Broken()
    app.request_refresh()
    assert app._wake.is_set() is True
