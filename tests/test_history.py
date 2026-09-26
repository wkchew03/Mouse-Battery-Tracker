"""Battery history sampling and drain-rate estimation."""

from mbt import history
from mbt.drivers.base import Reading
from mbt.store import Store

HOUR = 3600.0


def _series(pairs):
    """[(hours_ago_offset, percent)] -> history samples starting at t=0."""
    return [[hours * HOUR, percent] for hours, percent in pairs]


# --------------------------------------------------------------------------
# Sampling policy -- this is what keeps the feature cheap
# --------------------------------------------------------------------------


def test_only_changes_are_recorded():
    """Recording every poll would grow the file 60x faster and add nothing."""
    samples = history.record([], 0, 80)
    samples = history.record(samples, 60, 80)
    samples = history.record(samples, 120, 80)
    assert len(samples) == 1

    samples = history.record(samples, 180, 79)
    assert len(samples) == 2


def test_missing_percentage_is_not_recorded():
    """A charging mouse can report no level; that is not a data point."""
    assert history.record([], 0, None) == []


def test_history_is_bounded():
    samples = []
    for tick in range(history.MAX_SAMPLES * 2):
        samples = history.record(samples, tick * 60, tick % 101)
    assert len(samples) <= history.MAX_SAMPLES


def test_record_does_not_mutate_the_input():
    original = history.record([], 0, 90)
    history.record(original, 60, 89)
    assert len(original) == 1


# --------------------------------------------------------------------------
# Discharge window
# --------------------------------------------------------------------------


def test_window_stops_at_a_recharge():
    """Averaging across a recharge would show the mouse gaining battery."""
    samples = _series([(0, 90), (1, 85), (2, 40), (3, 100), (4, 98), (5, 96)])
    window = history.discharge_window(samples)
    assert [s[1] for s in window] == [100, 98, 96]


def test_window_is_everything_when_never_charged():
    samples = _series([(0, 90), (1, 88), (2, 86)])
    assert len(history.discharge_window(samples)) == 3


# --------------------------------------------------------------------------
# Rate estimation -- refuses to guess
# --------------------------------------------------------------------------


def test_rate_over_a_clean_discharge():
    samples = _series([(0, 100), (10, 90)])  # 10 points over 10 hours
    assert history.drain_rate(samples) == 1.0


def test_no_rate_without_enough_time():
    """Ten minutes of data must not produce a confident projection."""
    samples = [[0, 100], [600, 97]]
    assert history.drain_rate(samples) is None


def test_no_rate_without_enough_drop():
    samples = _series([(0, 100), (5, 99)])
    assert history.drain_rate(samples) is None


def test_no_rate_from_a_single_sample():
    assert history.drain_rate([[0, 90]]) is None
    assert history.drain_rate([]) is None


def test_rate_ignores_the_previous_cycle():
    samples = _series([(0, 100), (20, 20), (21, 100), (31, 90)])
    # Only the last 10 hours count: 10 points over 10 hours.
    assert history.drain_rate(samples) == 1.0


# --------------------------------------------------------------------------
# Projection and phrasing
# --------------------------------------------------------------------------


def test_hours_remaining():
    samples = _series([(0, 100), (10, 90)])  # 1%/h
    assert history.hours_remaining(samples, 50) == 50.0


def test_describe_remaining_units():
    assert history.describe_remaining(0.5) == "under 1h left"
    assert history.describe_remaining(12) == "~12h left"
    assert history.describe_remaining(72) == "~3d left"
    assert history.describe_remaining(None) is None


def test_describe_rate_switches_to_per_day_when_slow():
    assert history.describe_rate(2.0) == "2.0%/h"
    assert "day" in history.describe_rate(0.2)
    assert history.describe_rate(None) is None


def test_summary_is_none_until_there_is_evidence():
    assert history.summary([], 90) is None
    samples = _series([(0, 100), (10, 90)])
    assert history.summary(samples, 90) is not None


# --------------------------------------------------------------------------
# Store integration
# --------------------------------------------------------------------------


def test_store_records_history_on_change(tmp_path):
    store = Store(directory=tmp_path)
    store.update("m", "Mouse", Reading(online=True, percent=90))
    store.update("m", "Mouse", Reading(online=True, percent=90))
    store.update("m", "Mouse", Reading(online=True, percent=89))
    assert len(store.records["m"].history) == 2


def test_offline_readings_do_not_pollute_history(tmp_path):
    store = Store(directory=tmp_path)
    store.update("m", "Mouse", Reading(online=True, percent=90))
    store.update("m", "Mouse", Reading(online=False))
    assert len(store.records["m"].history) == 1


def test_history_survives_a_save_and_load(tmp_path):
    store = Store(directory=tmp_path)
    store.update("m", "Mouse", Reading(online=True, percent=90))
    store.update("m", "Mouse", Reading(online=True, percent=88))
    store.save()

    reloaded = Store(directory=tmp_path)
    reloaded.load()
    assert len(reloaded.records["m"].history) == 2


def test_records_saved_before_history_existed_still_load(tmp_path):
    """Old state.json files have no history key at all."""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(
        '{"m": {"label": "Mouse", "percent": 55, "last_online": 1.0}}',
        encoding="utf-8",
    )
    store = Store(directory=tmp_path)
    store.load()
    assert store.records["m"].percent == 55
    assert store.records["m"].history == []
