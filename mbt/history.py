"""Battery history and drain-rate estimation.

Pure functions over a list of `[timestamp, percent]` pairs, so the policy is
testable without a store, a device or a clock.

The design keeps this cheap. A sample is recorded only when the percentage
actually *changes*, never once per poll: battery moves slowly, so a mouse in
use produces roughly one sample an hour rather than sixty. Transitions are also
exactly what a rate estimate needs -- the time spent at each level -- so nothing
is lost by dropping the repeats.

Estimates deliberately refuse to answer rather than guess. A number invented
from ten minutes of data would look just as authoritative as a good one, and
"~3d left" being wrong is worse than showing nothing.
"""

from __future__ import annotations

# Roughly a week of transitions for a mouse that drains a few points a day.
MAX_SAMPLES = 120

# Estimation guards.
MIN_SPAN_SECONDS = 30 * 60
MIN_DROP_PERCENT = 2

Sample = list  # [timestamp: float, percent: int]


def should_record(history: list[Sample], percent: int | None) -> bool:
    """True when this reading adds information.

    Only changes are kept. Recording every poll would grow the file sixty times
    faster while telling us nothing new between transitions.
    """
    if percent is None:
        return False
    if not history:
        return True
    return history[-1][1] != percent


def record(
    history: list[Sample], timestamp: float, percent: int | None
) -> list[Sample]:
    """Append a sample if it is worth keeping, returning a bounded history."""
    if not should_record(history, percent):
        return history
    updated = list(history)
    updated.append([float(timestamp), int(percent)])
    if len(updated) > MAX_SAMPLES:
        updated = updated[-MAX_SAMPLES:]
    return updated


def discharge_window(history: list[Sample]) -> list[Sample]:
    """The most recent run of non-increasing samples.

    Anything before the last rise belongs to a previous charge cycle. Averaging
    across a recharge would report a mouse as gaining battery, or cancel a real
    drain out to nothing.
    """
    if len(history) < 2:
        return list(history)
    start = 0
    for index in range(len(history) - 1, 0, -1):
        if history[index][1] > history[index - 1][1]:
            start = index
            break
    return history[start:]


def drain_rate(history: list[Sample]) -> float | None:
    """Percent lost per hour over the current discharge, or None if unknown."""
    window = discharge_window(history)
    if len(window) < 2:
        return None

    span = window[-1][0] - window[0][0]
    drop = window[0][1] - window[-1][1]
    if span < MIN_SPAN_SECONDS or drop < MIN_DROP_PERCENT:
        return None

    rate = drop / (span / 3600.0)
    return rate if rate > 0 else None


def hours_remaining(history: list[Sample], percent: int | None) -> float | None:
    rate = drain_rate(history)
    if rate is None or percent is None:
        return None
    return percent / rate


def describe_rate(rate: float | None) -> str | None:
    """Human phrasing for a drain rate.

    Per-day reads better than per-hour for anything slower than a point an
    hour, which is most of these mice.
    """
    if rate is None:
        return None
    if rate >= 1:
        return f"{rate:.1f}%/h"
    per_day = rate * 24
    if per_day < 0.1:
        return None
    return f"{per_day:.1f}%/day"


def describe_remaining(hours: float | None) -> str | None:
    if hours is None:
        return None
    if hours < 1:
        return "under 1h left"
    if hours < 48:
        return f"~{round(hours)}h left"
    return f"~{round(hours / 24)}d left"


def summary(history: list[Sample], percent: int | None) -> str | None:
    """One line combining rate and time remaining, or None when unknown."""
    rate_text = describe_rate(drain_rate(history))
    if rate_text is None:
        return None
    remaining = describe_remaining(hours_remaining(history, percent))
    return f"{rate_text} · {remaining}" if remaining else rate_text


def merge(a: list[Sample], b: list[Sample]) -> list[Sample]:
    """Combine two histories from the same physical mouse into one timeline.

    Needed when a device's identity key changes (e.g. Logitech's serial-keyed
    record folding into its unit-id one once a live read resolves it) --
    without this, `merge_aliases` picking "whichever record is newer" silently
    drops the other one's samples, and a mouse with months of drain-rate data
    reverts to looking brand new.
    """
    combined = {round(float(t), 3): int(p) for t, p in a}
    combined.update({round(float(t), 3): int(p) for t, p in b})
    merged = [[t, p] for t, p in sorted(combined.items())]
    if len(merged) > MAX_SAMPLES:
        merged = merged[-MAX_SAMPLES:]
    return merged


def discharge_series(history: list[Sample], limit: int = 40) -> list[tuple[float, int]]:
    """(seconds from the window's start, percent) for the current discharge.

    Timestamps are kept because samples are recorded only when the level *changes*,
    so they are irregularly spaced in time. Plotting them evenly would draw a
    mouse that sat untouched all afternoon with the same slope as one being
    used, and misreport the drain the chart exists to show.
    """
    window = discharge_window(history)
    if len(window) < 2:
        return []
    if len(window) > limit:
        window = window[-limit:]
    start = float(window[0][0])
    return [(float(stamp) - start, int(percent)) for stamp, percent in window]
