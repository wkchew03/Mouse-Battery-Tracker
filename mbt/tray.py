"""Tray icon, menu, and the refresh loop that drives them.

The tray thread never touches HID. It only consumes whatever the provider
callable hands back, so a wedged device can never freeze the UI.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable

import pystray
from PIL import Image, ImageDraw  # noqa: F401  (Image used for LANCZOS)

from . import autostart, feed
from .cards import load_font
from .drivers.base import Reading
from .store import ALERT_CLEAR_MARGIN, DEFAULT_SETTINGS, Store, format_age
from .theme import (  # noqa: F401  (re-exported for callers and tests)
    COLOR_CHARGING,
    COLOR_HIGH,
    COLOR_LOW,
    COLOR_MID,
    COLOR_UNKNOWN,
    level_color,
)

ICON_SIZE = 64

# Diagnostic log. pystray swallows exceptions raised inside menu callbacks and
# the app runs under pythonw with no console, so without a file there is no way
# to see whether a menu click did anything at all.
_LOG_PATH = None
_LOG_LIMIT = 128 * 1024


def debug_log(message: str) -> None:
    global _LOG_PATH
    # Tests construct TrayApp against a temp store, but this path is absolute,
    # so without this they scribble their fake failures into the real log.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        if _LOG_PATH is None:
            from .store import app_dir

            _LOG_PATH = app_dir() / "tray.log"
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _LOG_LIMIT:
            _LOG_PATH.unlink()
        stamp = time.strftime("%H:%M:%S")
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass

# Warn once per discharge cycle, not every poll. The mouse must climb back above
# the clear level before it can warn again, so a level hovering on the
# threshold doesn't produce a notification every minute.
#
# These are only the defaults now -- the live values come from the store, which
# the HUD writes to. Kept as names because they are what "off the shelf" means.
LOW_BATTERY_THRESHOLD = DEFAULT_SETTINGS["alert_threshold"]
LOW_BATTERY_CLEAR = LOW_BATTERY_THRESHOLD + ALERT_CLEAR_MARGIN

# (key, label, Reading) for every device the app currently knows how to reach.
Provider = Callable[[], Iterable[tuple[str, str, Reading]]]


def render_icon(percent: int | None, charging: bool = False) -> Image.Image:
    """Percentage on a level-coloured tile.

    Drawn oversized and downscaled: Pillow does not antialias rounded_rectangle,
    so rendering at ICON_SIZE directly leaves visibly stepped corners once the
    shell resamples it for the tray.
    """
    scale = 4
    size = ICON_SIZE * scale
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (2 * scale, 2 * scale, size - 3 * scale, size - 3 * scale),
        radius=14 * scale,
        fill=level_color(percent, charging),
    )

    text = "--" if percent is None else str(percent)
    font = load_font((40 if len(text) <= 2 else 28) * scale)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    position = (
        (size - (right - left)) / 2 - left,
        (size - (bottom - top)) / 2 - top,
    )
    draw.text(position, text, font=font, fill=(255, 255, 255, 255))
    return image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)


class TrayApp:
    def __init__(
        self,
        store: Store,
        provider: Provider,
        poll_interval: float = 60.0,
        hud=None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.poll_interval = poll_interval
        self.hud = hud
        # Set by run_tray when a real Poller is in use, so a manual refresh can
        # clear its backoff. None under mock providers.
        self.poller = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._online: list[tuple[str, str, Reading]] = []
        # Device keys already warned about in their current discharge cycle.
        self._warned: set[str] = set()

        self.icon = pystray.Icon(
            "mbt",
            icon=render_icon(None),
            title="Mouse Battery Tracker",
            menu=pystray.Menu(*self._menu_items()),
        )

    # ---- menu -----------------------------------------------------------

    def _menu_items(self) -> list[pystray.MenuItem]:
        items: list[pystray.MenuItem] = []
        with self._lock:
            online = list(self._online)

        online_keys = {key for key, _, _ in online}

        items.append(pystray.MenuItem("Connected", None, enabled=False))
        if online:
            for key, label, reading in online:
                name = self.store.display_name(key, label)
                items.append(
                    pystray.MenuItem(f"    {name} — {reading.describe()}", None, enabled=False)
                )
        else:
            items.append(pystray.MenuItem("    no mouse detected", None, enabled=False))

        recent = [r for r in self.store.recent(exclude=online_keys) if r.last_online]
        if recent:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Recently used", None, enabled=False))
            now = time.time()
            for record in recent[:6]:
                name = self.store.display_name(record.key, record.label)
                level = record.describe_last_known()
                age = format_age(now - record.last_online)
                items.append(
                    pystray.MenuItem(f"    {name} — {level} · {age}", None, enabled=False)
                )

        items.append(pystray.Menu.SEPARATOR)
        if self.hud is not None:
            # default=True also makes this fire on left-click / double-click.
            items.append(pystray.MenuItem("Show details", self._on_show_hud, default=True))
        items.append(pystray.MenuItem("Refresh now", self._on_refresh))
        items.append(
            pystray.MenuItem(
                "Start with Windows",
                self._on_toggle_autostart,
                checked=lambda _item: autostart.is_enabled(),
            )
        )
        items.append(pystray.MenuItem("Quit", self._on_quit))
        return items

    def _check_low_battery(self, online: list[tuple[str, str, Reading]]) -> None:
        """Notify once when a mouse drops below the threshold.

        Charging clears the warning immediately -- a mouse on the cable is no
        longer a problem even if it is still reading low.
        """
        if not self.store.notify_low:
            # Forget who has been warned, so re-enabling notifications warns
            # about a mouse that dropped low while they were off.
            self._warned.clear()
            return

        threshold = self.store.alert_threshold
        clear = self.store.alert_clear

        for key, label, reading in online:
            percent = reading.percent
            if percent is None:
                continue
            if reading.charging or percent >= clear:
                self._warned.discard(key)
                continue
            if percent <= threshold and key not in self._warned:
                self._warned.add(key)
                name = self.store.display_name(key, label)
                try:
                    self.icon.notify(f"{name} is at {percent}%", "Low mouse battery")
                except Exception:
                    # Notifications are best-effort; never break the poll loop.
                    pass

    def _refresh_ui(self) -> None:
        with self._lock:
            online = list(self._online)

        primary = next((r for _, _, r in online if r.percent is not None), None)
        if primary is None and online:
            primary = online[0][2]

        if primary is not None:
            self.icon.icon = render_icon(primary.percent, bool(primary.charging))
        else:
            self.icon.icon = render_icon(None)

        if online:
            parts = [
                f"{self.store.display_name(k, lbl)}: {r.describe()}" for k, lbl, r in online
            ]
            self.icon.title = "\n".join(["Mouse Battery Tracker"] + parts)[:127]
        else:
            self.icon.title = "Mouse Battery Tracker — no mouse detected"

        if self.hud is not None:
            self.hud.update(online)

        self.icon.menu = pystray.Menu(*self._menu_items())
        # update_menu() only does anything once the icon is running; calling it
        # before run() (or during shutdown) must not take the poll thread down.
        try:
            self.icon.update_menu()
        except Exception:
            pass

    # ---- polling --------------------------------------------------------

    def poll_once(self) -> None:
        started = time.time()
        try:
            results = list(self.provider())
        except Exception as exc:
            debug_log(f"poll failed: {exc}")
            results = []
        live = [label for _, label, reading in results if reading.online]
        debug_log(
            f"poll took {time.time() - started:.2f}s; "
            f"{len(results)} device(s); online={live}"
        )

        online = []
        for key, label, reading in results:
            self.store.update(key, label, reading)
            if reading.online:
                online.append((key, label, reading))

        # Some identities are only knowable after a live read (Logitech's unit
        # id), so a mouse can still be sitting under its old key the first
        # time this runs at startup. Cheap when there is nothing to merge --
        # merge_aliases only touches disk when it actually folds something --
        # so it is safe to check again after every poll rather than once.
        self._merge_legacy_aliases()

        with self._lock:
            self._online = online

        try:
            self.store.save()
        except OSError:
            pass

        # Publish for external readers (the Stream Deck plugin). Best-effort:
        # a feed problem must never stop the tray updating.
        try:
            feed.publish(self.store, {key for key, _, _ in online})
        except Exception:
            pass

        self._check_low_battery(online)
        self._refresh_ui()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            # Wake early when the user asks for a manual refresh.
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    # ---- actions --------------------------------------------------------

    def request_refresh(self, source: str = "?") -> None:
        """Poll now, ignoring backoff. Safe to call from any thread."""
        poller = getattr(self, "poller", None)
        debug_log(f"request_refresh from {source}; poller={poller is not None}")
        if poller is not None:
            try:
                poller.reset_backoff()
            except Exception as exc:
                debug_log(f"  reset_backoff failed: {exc}")

        # A poll is not instant, so say something immediately. Without this a
        # click on "Refresh now" looks like it did nothing at all.
        try:
            self.icon.title = "Mouse Battery Tracker — refreshing…"
        except Exception:
            pass

        self._wake.set()

    def _on_refresh(self, icon=None, item=None) -> None:
        self.request_refresh("menu")

    def _on_show_hud(self, icon=None, item=None) -> None:
        if self.hud is not None:
            self.hud.show()

    def _merge_legacy_aliases(self) -> None:
        try:
            from .app import legacy_aliases

            self.store.merge_aliases(legacy_aliases())
        except Exception:
            pass

    def _on_toggle_autostart(self, icon=None, item=None) -> None:
        autostart.toggle()
        self._refresh_ui()

    def _on_quit(self, icon=None, item=None) -> None:
        self._stop.set()
        self._wake.set()
        if self.hud is not None:
            self.hud.stop()
        self.icon.stop()

    def run(self) -> None:
        self.store.load()
        self._merge_legacy_aliases()
        if self.hud is not None:
            self.hud.start()
        thread = threading.Thread(target=self._loop, name="mbt-poll", daemon=True)
        thread.start()
        self.icon.run()
