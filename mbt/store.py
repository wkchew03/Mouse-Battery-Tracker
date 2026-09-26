"""Persistence for last-known battery levels and user-assigned names.

Two files under %APPDATA%\\MouseBatteryTracker:

- state.json  last known reading per device, so a mouse that is currently off
              can still be shown under "Recent" with its last level and when it
              was last seen.
- names.json  user-assigned display names. These devices report useless product
              strings ("PIAO-2.4G", "Gaming Mouse 8K"), so the UI needs a way to
              call a mouse what the user calls it.
- settings.json  the handful of preferences the HUD exposes. Separate from
              state.json so toggling a checkbox does not rewrite the whole
              history file, which is two orders of magnitude larger.

Writes are atomic (temp file + os.replace) because the tray app can be killed at
any moment, and a half-written state file would lose every device's history.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import APP_NAME, history
from .drivers.base import Reading


# Preferences the HUD exposes. Defined here rather than in tray.py so the HUD
# can read and write them without importing the tray -- the two run on
# different threads and must not depend on each other.
DEFAULT_SETTINGS = {
    # Percentage at which the low-battery notification fires.
    "alert_threshold": 15,
    # Whether it fires at all.
    "notify_low": True,
}

# How far a mouse must climb back above the threshold before it can warn again.
# Without the margin a level resting on the boundary notifies every poll.
ALERT_CLEAR_MARGIN = 10

# A threshold outside this range is not a preference, it is a mistake: 0 never
# fires and 90 fires constantly.
MIN_ALERT_THRESHOLD = 5
MAX_ALERT_THRESHOLD = 50


def clamp_threshold(value: int) -> int:
    return max(MIN_ALERT_THRESHOLD, min(MAX_ALERT_THRESHOLD, int(value)))


def app_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_NAME


@dataclass
class DeviceRecord:
    """What we remember about one physical mouse between runs."""

    key: str
    label: str = ""
    percent: int | None = None
    charging: bool | None = None
    bucket: str | None = None
    # Last time the mouse answered a battery query (i.e. was powered on).
    last_online: float = 0.0
    # Last time we recorded anything at all for it.
    last_update: float = field(default_factory=time.time)
    # [[timestamp, percent], ...] recorded on change only; see history.py.
    # Records saved before this existed simply start empty.
    history: list = field(default_factory=list)

    def describe_last_known(self) -> str:
        """Last known level, for the "Recently used" list.

        Deliberately not routed through `Reading.describe()`: a stored record
        always has online=False (it describes the past), and describe() reports
        "off" for that, which would hide the very number this list exists to
        show.
        """
        if self.percent is not None:
            return f"{self.percent}%"
        if self.bucket:
            return self.bucket
        return "unknown"


def format_age(seconds: float) -> str:
    """Compact relative time for the tray menu."""
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    # Days all the way up rather than switching to weeks: "3w ago" reads as
    # vaguer than it is, and the exact figure is the point of the line -- it is
    # how you tell a mouse you rotated out last month from one you lost.
    return f"{int(hours / 24)}d ago"


class Store:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or app_dir()
        self.state_file = self.directory / "state.json"
        self.names_file = self.directory / "names.json"
        self.settings_file = self.directory / "settings.json"
        self.records: dict[str, DeviceRecord] = {}
        self.names: dict[str, str] = {}
        self.settings: dict = dict(DEFAULT_SETTINGS)

    # ---- io -------------------------------------------------------------

    def load(self) -> None:
        self.records = {}
        for key, raw in _read_json(self.state_file, {}).items():
            if not isinstance(raw, dict):
                continue
            fields = {k: v for k, v in raw.items() if k in DeviceRecord.__annotations__}
            fields["key"] = key
            try:
                self.records[key] = DeviceRecord(**fields)
            except TypeError:
                continue
        names = _read_json(self.names_file, {})
        self.names = {k: v for k, v in names.items() if isinstance(v, str)}
        self.settings = self._load_settings()

    def _load_settings(self) -> dict:
        """Defaults overlaid with whatever the file has, field by field.

        A settings file written by a newer version, hand-edited, or truncated
        mid-write must not take the app down or silently disable an alert, so
        every value is validated on its own and a bad one falls back.
        """
        settings = dict(DEFAULT_SETTINGS)
        raw = _read_json(self.settings_file, {})
        if not isinstance(raw, dict):
            return settings
        if isinstance(raw.get("notify_low"), bool):
            settings["notify_low"] = raw["notify_low"]
        try:
            settings["alert_threshold"] = clamp_threshold(raw["alert_threshold"])
        except (KeyError, TypeError, ValueError):
            pass
        return settings

    def save_settings(self) -> None:
        _write_json(self.settings_file, self.settings)

    # ---- settings accessors ---------------------------------------------

    @property
    def alert_threshold(self) -> int:
        return clamp_threshold(self.settings.get("alert_threshold", 15))

    @property
    def alert_clear(self) -> int:
        """Level at which a warned mouse becomes eligible to warn again."""
        return min(100, self.alert_threshold + ALERT_CLEAR_MARGIN)

    @property
    def notify_low(self) -> bool:
        return bool(self.settings.get("notify_low", True))

    def set_alert_threshold(self, value: int) -> int:
        self.settings["alert_threshold"] = clamp_threshold(value)
        self.save_settings()
        return self.settings["alert_threshold"]

    def set_notify_low(self, enabled: bool) -> None:
        self.settings["notify_low"] = bool(enabled)
        self.save_settings()

    def save(self) -> None:
        payload = {}
        for key, record in self.records.items():
            data = asdict(record)
            data.pop("key", None)
            payload[key] = data
        _write_json(self.state_file, payload)

    def save_names(self) -> None:
        _write_json(self.names_file, self.names)

    # ---- mutation -------------------------------------------------------

    def update(self, key: str, label: str, reading: Reading) -> DeviceRecord:
        """Fold one reading into the record for `key`.

        A failed or offline read never overwrites a known percentage -- losing
        the last good value would make the "Recent" list useless.
        """
        record = self.records.get(key) or DeviceRecord(key=key)
        now = time.time()
        if label:
            record.label = label
        record.last_update = now
        if reading.online:
            record.last_online = now
            if reading.percent is not None:
                record.percent = reading.percent
                record.history = history.record(record.history, now, reading.percent)
            if reading.bucket is not None:
                record.bucket = reading.bucket
            record.charging = reading.charging
        self.records[key] = record
        return record

    def merge_aliases(self, aliases: dict[str, str]) -> int:
        """Fold records and names stored under old keys into their new identity.

        Called at startup after entries were merged, so history saved under the
        per-USB-device keys doesn't linger as a stale duplicate. The newer
        record wins; the older one only fills gaps.
        """
        merged = 0
        for old_key, new_key in aliases.items():
            if old_key == new_key or old_key not in self.records:
                continue
            old = self.records.pop(old_key)
            merged += 1
            existing = self.records.get(new_key)
            if existing is None:
                old.key = new_key
                self.records[new_key] = old
            else:
                # Whichever side actually answered more recently wins the
                # live fields (percent, charging, ...), but the combined
                # timeline is kept either way -- otherwise the record that
                # loses gets its whole drain-rate history discarded, and a
                # mouse with months of samples looks brand new.
                merged_history = history.merge(old.history, existing.history)
                if old.last_online > existing.last_online:
                    old.key = new_key
                    old.label = existing.label or old.label
                    old.history = merged_history
                    self.records[new_key] = old
                else:
                    existing.history = merged_history
            if old_key in self.names:
                self.names.setdefault(new_key, self.names.pop(old_key))
        if merged:
            try:
                self.save()
                self.save_names()
            except OSError:
                pass
        return merged

    def display_name(self, key: str, fallback: str = "") -> str:
        return self.names.get(key) or fallback or key

    def set_display_name(self, key: str, name: str) -> None:
        name = name.strip()
        if name:
            self.names[key] = name
        else:
            self.names.pop(key, None)
        self.save_names()

    def recent(self, exclude: set[str] | None = None) -> list[DeviceRecord]:
        """Records not currently online, most recently used first."""
        exclude = exclude or set()
        items = [r for k, r in self.records.items() if k not in exclude]
        items.sort(key=lambda r: r.last_online, reverse=True)
        return items


def _read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)
