"""Per-device polling with exponential backoff.

A mouse that is switched off still has its dongle enumerated, so it would be
retried on every cycle forever. Each failure pushes that device's next attempt
further out (60s -> 120s -> 240s -> capped at 300s), which keeps a pile of
sleeping mice from costing anything while gaming. Any success resets it.

Devices that are backed off report their last known reading, so the tray does
not flicker between "41%" and "off" while a mouse is asleep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .app import discover
from .drivers.base import (
    OFFLINE,
    Reading,
    collapse_duplicates,
    device_key,
    resolve_identity,
    resolve_label,
)
from .store import debug_log


@dataclass
class _DeviceState:
    failures: int = 0
    next_due: float = 0.0
    last: Reading = field(default_factory=lambda: OFFLINE)
    label: str = ""


class Poller:
    def __init__(
        self,
        base_interval: float = 60.0,
        max_interval: float = 300.0,
        failure_threshold: int = 3,
    ) -> None:
        self.base_interval = base_interval
        self.max_interval = max_interval
        self.failure_threshold = failure_threshold
        self._state: dict[str, _DeviceState] = {}

    def backoff_for(self, failures: int) -> float:
        """Interval after `failures` consecutive failures."""
        if failures < self.failure_threshold:
            return self.base_interval
        extra = failures - self.failure_threshold + 1
        return min(self.base_interval * (2**extra), self.max_interval)

    def reset_backoff(self) -> None:
        """Make every device due immediately.

        A manual refresh has to clear backoff, not just wake the loop: a mouse
        that was asleep may be several failures deep and not due for minutes,
        so without this "refresh" would return the same cached "off" and the
        mouse would stay invisible despite being switched on.
        """
        for state in self._state.values():
            state.failures = 0
            state.next_due = 0.0

    def poll(self, now: float | None = None) -> list[tuple[str, str, Reading]]:
        """Read every due device; reuse the cached reading for the rest."""
        now = time.time() if now is None else now
        results: list[tuple[str, str, Reading]] = []
        seen: set[str] = set()

        for driver, info in discover():
            # Backoff is tracked per USB device, but results are reported under
            # the physical-mouse identity so wired and wireless forms of one
            # mouse collapse into a single entry.
            hardware_key = device_key(info)
            key = resolve_identity(driver, info)
            seen.add(hardware_key)
            label = resolve_label(driver, info)
            state = self._state.setdefault(hardware_key, _DeviceState())
            state.label = label

            if now < state.next_due:
                results.append((state.last.device_id or key, label, state.last))
                continue

            try:
                reading = driver.read(info)
            except Exception as exc:
                # Logged because a parse bug (an IndexError on a short reply)
                # otherwise looks exactly like a mouse switched off, and is
                # backed off rather than noticed.
                debug_log(f"{driver.name} read failed on {hardware_key}: {exc!r}")
                reading = OFFLINE

            if reading.online:
                state.failures = 0
            else:
                state.failures += 1

            state.last = reading
            state.next_due = now + self.backoff_for(state.failures)
            # A device that can name itself wins over the USB-derived key.
            results.append((reading.device_id or key, label, reading))

        # Drop state for devices that are no longer present at all, so a
        # replugged dongle starts from a clean interval.
        for key in list(self._state):
            if key not in seen:
                del self._state[key]

        return collapse_duplicates(results)
