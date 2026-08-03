"""Publishes a small feed for external consumers (the Stream Deck plugin).

Deliberately a plain directory of files rather than a socket or HTTP server: the
consumer is a separate process that only ever reads, a file feed needs no port,
no firewall prompt, and no lifecycle coordination, and it survives the tray app
restarting.

    %APPDATA%/MouseBatteryTracker/streamdeck/
        feed.json          every mouse, newest reading first
        icons/<key>.png    pre-rendered gauge, so the plugin draws nothing

`generated_at` lets a reader tell "the app is running and this mouse is at 96%"
from "the app died an hour ago and this is what it last saw" -- without it a
stale figure looks exactly like a live one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .mouseart import load_custom, render_mouse, safe_filename
from .store import Store, format_age

FEED_VERSION = 1
ICON_SIZE = 144  # Stream Deck keys are 72px standard, 144px on newer hardware.


def feed_dir(store: Store) -> Path:
    return store.directory / "streamdeck"


def icon_name(key: str) -> str:
    return f"{safe_filename(key.replace(':', '_'))}.png"


def build_payload(store: Store, online_keys: set[str] | None = None) -> dict:
    """Snapshot of every known mouse, connected ones first."""
    online_keys = online_keys or set()
    now = time.time()

    entries = []
    for key, record in store.records.items():
        connected = key in online_keys
        entries.append(
            {
                "key": key,
                "name": store.display_name(key, record.label),
                "percent": record.percent,
                "charging": bool(record.charging),
                "connected": connected,
                "last_seen": record.last_online,
                "last_seen_text": (
                    "now" if connected else format_age(now - record.last_online)
                ),
                "icon": icon_name(key),
            }
        )

    # Connected first, then most recently seen.
    entries.sort(key=lambda e: (not e["connected"], -e["last_seen"]))
    return {
        "version": FEED_VERSION,
        "generated_at": now,
        "mice": entries,
    }


def write_icons(store: Store, payload: dict) -> None:
    """Render one icon per mouse.

    A user-supplied image (the same `images/` folder the HUD uses) wins over the
    drawn gauge, so a photo dropped in for the HUD shows on the Stream Deck too
    rather than needing to be added twice.
    """
    icons = feed_dir(store) / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    custom_dir = store.directory / "images"

    for entry in payload["mice"]:
        image = load_custom(custom_dir, entry["key"], ICON_SIZE, entry["name"])
        if image is None:
            image = render_mouse(
                size=ICON_SIZE,
                percent=entry["percent"],
                charging=entry["charging"],
                online=entry["connected"],
            )
        target = icons / entry["icon"]
        tmp = target.with_suffix(".png.tmp")
        try:
            image.save(tmp, "PNG")
            os.replace(tmp, target)
        except OSError:
            continue


def publish(store: Store, online_keys: set[str] | None = None) -> dict:
    """Write feed.json and the icons. Never raises."""
    payload = build_payload(store, online_keys)
    directory = feed_dir(store)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_icons(store, payload)

        target = directory / "feed.json"
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except OSError:
        pass
    return payload
