"""Backoff behaviour. Uses a fake driver so no hardware is touched."""

import pytest

from mbt import poller as poller_module
from mbt.drivers.base import OFFLINE, Reading
from mbt.poller import Poller


class FakeDriver:
    name = "fake"

    def __init__(self, reading):
        self.reading = reading
        self.reads = 0

    def candidates(self, infos):
        return []

    def read(self, info):
        self.reads += 1
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


INFO = {
    "vendor_id": 0x1532,
    "product_id": 0x007B,
    "serial_number": "",
    "path": b"fake",
    "product_string": "Fake Mouse",
    "manufacturer_string": "",
}


@pytest.fixture
def patched(monkeypatch):
    def install(driver):
        monkeypatch.setattr(poller_module, "discover", lambda: [(driver, INFO)])
        return driver

    return install


def test_backoff_schedule():
    p = Poller(base_interval=60, max_interval=300, failure_threshold=3)
    assert p.backoff_for(0) == 60
    assert p.backoff_for(2) == 60      # below the threshold, normal interval
    assert p.backoff_for(3) == 120
    assert p.backoff_for(4) == 240
    assert p.backoff_for(5) == 300     # capped
    assert p.backoff_for(50) == 300


def test_success_polls_every_interval(patched):
    driver = patched(FakeDriver(Reading(online=True, percent=80)))
    p = Poller(base_interval=60)
    p.poll(now=0)
    p.poll(now=60)
    p.poll(now=120)
    assert driver.reads == 3


def test_offline_device_is_backed_off(patched):
    driver = patched(FakeDriver(OFFLINE))
    p = Poller(base_interval=60, failure_threshold=3)

    # First three polls happen at the normal interval.
    for now in (0, 60, 120):
        p.poll(now=now)
    assert driver.reads == 3

    # The third failure hits the threshold and stretches the interval to 120s,
    # so the next ordinary 60s tick must be skipped rather than retried.
    p.poll(now=180)
    assert driver.reads == 3, "backed-off device was polled too early"
    p.poll(now=240)
    assert driver.reads == 4

    # Fourth failure stretches it again, to 240s.
    p.poll(now=300)
    assert driver.reads == 4
    p.poll(now=480)
    assert driver.reads == 5


def test_backed_off_device_keeps_reporting_last_reading(patched):
    driver = patched(FakeDriver(Reading(online=True, percent=77)))
    p = Poller(base_interval=60)
    p.poll(now=0)

    driver.reading = OFFLINE
    for now in (60, 120, 180):
        p.poll(now=now)

    # Skipped cycle must reuse the cached value, not vanish from the list.
    results = p.poll(now=200)
    assert len(results) == 1
    assert driver.reads == 4


def test_exception_counts_as_failure(patched):
    driver = patched(FakeDriver(RuntimeError("device wedged")))
    p = Poller(base_interval=60, failure_threshold=1)
    results = p.poll(now=0)
    assert results[0][2].online is False


def test_read_exception_is_logged(patched, monkeypatch):
    """A parse bug must not pass silently as a mouse that is switched off."""
    logged = []
    monkeypatch.setattr(poller_module, "debug_log", logged.append)
    patched(FakeDriver(IndexError("reply too short")))
    Poller().poll(now=0)
    assert len(logged) == 1
    assert "fake" in logged[0] and "IndexError" in logged[0]


def test_success_resets_backoff(patched):
    driver = patched(FakeDriver(OFFLINE))
    p = Poller(base_interval=60, failure_threshold=1)
    p.poll(now=0)
    p.poll(now=200)
    driver.reading = Reading(online=True, percent=50)
    p.poll(now=1000)
    # After a success the device is due again one base interval later.
    p.poll(now=1060)
    assert driver.reads == 4


def test_absent_device_state_is_dropped(monkeypatch):
    driver = FakeDriver(Reading(online=True, percent=50))
    monkeypatch.setattr(poller_module, "discover", lambda: [(driver, INFO)])
    p = Poller()
    p.poll(now=0)
    assert p._state

    monkeypatch.setattr(poller_module, "discover", lambda: [])
    p.poll(now=1)
    assert not p._state, "state for an unplugged device should be cleared"
