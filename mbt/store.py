"""Persistence for last-known battery levels and user-assigned names.

Two files under %APPDATA%\\MouseBatteryTracker:

- state.json  last known reading per device, so a mouse that is currently off
              can still be shown under "Recent" with its last level and when it
              was last seen.
- names.json  user-assigned display names. These devices report useless product
              strings ("PIAO-2.4G", "Gaming Mouse 8K"), so the UI needs a way to
              call a mouse what the user calls it.

Writes are atomic (temp file + os.replace) because the tray app can be killed at
any moment, and a half-written state file would lose every device's history.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import APP_NAME
from .drivers.base import Reading


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
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    return f"{int(days / 7)}w ago"


class Store:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or app_dir()
        self.state_file = self.directory / "state.json"
        self.names_file = self.directory / "names.json"
        self.records: dict[str, DeviceRecord] = {}
        self.names: dict[str, str] = {}

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
            elif old.last_online > existing.last_online:
                old.key = new_key
                old.label = existing.label or old.label
                self.records[new_key] = old
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
