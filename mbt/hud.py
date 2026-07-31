"""Detail window: one card per mouse, opened from the tray.

Threading: pystray owns the main thread on Windows, so Tk runs on its own
thread and owns everything it creates. Tk is not thread-safe, so the tray
thread never touches a widget -- it posts messages onto a queue which the Tk
thread drains from inside its own event loop via `after()`.

The window is created once and hidden with `withdraw()` rather than destroyed,
so reopening is instant and Tk never has to be re-initialised on a thread that
already ran a mainloop.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import dpi
from .drivers.base import Reading
from .mouseart import install_image, load_custom, render_mouse
from .store import Store, format_age
from .theme import (
    HUD_BG,
    HUD_CARD,
    HUD_CARD_DIM,
    HUD_MUTED,
    HUD_TEXT,
    HUD_TRACK,
    level_color,
    to_hex,
)

ART_SIZE = 72
WINDOW_WIDTH = 430
MAX_RECENT = 6


class Hud:
    """Owns the Tk thread. Safe to call show()/update()/stop() from anywhere."""

    def __init__(self, store: Store, images_dir: Path | None = None) -> None:
        self.store = store
        self.images_dir = images_dir or (store.directory / "images")
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._body = None
        # Tk garbage-collects images that nothing references, leaving blank
        # labels, so every PhotoImage in the current view is kept alive here.
        self._images: list = []
        self._online: list[tuple[str, str, Reading]] = []
        # Filled in on the Tk thread once the display DPI is known.
        self.scale = 1.0

    def px(self, value: float) -> int:
        """Device pixels for a value expressed at 100% scale."""
        return max(1, int(round(value * self.scale)))

    # ---- public API (any thread) ----------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mbt-hud", daemon=True)
        self._thread.start()

    def show(self) -> None:
        self._queue.put(("show",))

    def update(self, online: list[tuple[str, str, Reading]]) -> None:
        self._queue.put(("data", list(online)))

    def stop(self) -> None:
        self._queue.put(("quit",))

    # ---- Tk thread -------------------------------------------------------

    def _run(self) -> None:
        import tkinter as tk

        self._tk = tk
        root = tk.Tk()
        self._root = root
        root.title("Mouse Battery Tracker")
        root.configure(bg=HUD_BG)

        self.scale = dpi.scale()
        # Point-sized fonts render at the correct physical size only if Tk knows
        # how many pixels there are per point.
        root.tk.call("tk", "scaling", dpi.tk_scaling())

        root.geometry(f"{self.px(WINDOW_WIDTH)}x{self.px(560)}")
        root.minsize(self.px(WINDOW_WIDTH), self.px(260))

        # Closing the window hides it; the app keeps running in the tray.
        root.protocol("WM_DELETE_WINDOW", self._hide)
        root.bind("<Escape>", lambda _event: self._hide())

        self._body = tk.Frame(root, bg=HUD_BG)
        self._body.pack(
            fill="both", expand=True, padx=self.px(14), pady=self.px(14)
        )

        # Create it up front so the folder is there to drop images into,
        # rather than only appearing once someone clicks the link.
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        root.withdraw()
        root.after(120, self._drain)
        root.mainloop()

    def _drain(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "show":
                    self._show_now()
                elif kind == "data":
                    self._online = message[1]
                    if self._root is not None and self._root.state() != "withdrawn":
                        self._rebuild()
                elif kind == "quit":
                    if self._root is not None:
                        self._root.quit()
                        self._root.destroy()
                    return
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(150, self._drain)

    def _hide(self) -> None:
        if self._root is not None:
            self._root.withdraw()

    def _rename(self, key: str, current: str) -> None:
        """Prompt for a display name. Blank clears the override."""
        from tkinter import simpledialog

        answer = simpledialog.askstring(
            "Rename mouse",
            "Display name (leave blank to use the device's own name):",
            initialvalue=current,
            parent=self._root,
        )
        if answer is None:  # cancelled
            return
        self.store.set_display_name(key, answer)
        # The image is looked up by name, so carry the file across or a rename
        # would silently drop the picture.
        new_name = self.store.display_name(key, "")
        if new_name != current:
            self._move_image(key, current, new_name)
        self._rebuild()

    def _move_image(self, key: str, old_name: str, new_name: str) -> None:
        from .mouseart import candidate_paths, custom_image_path

        existing = next(
            (p for p in candidate_paths(self.images_dir, key, old_name) if p.exists()),
            None,
        )
        if existing is None:
            return
        target = custom_image_path(self.images_dir, key, new_name)
        if existing == target:
            return
        try:
            existing.replace(target)
        except OSError:
            pass

    def _choose_image(self, key: str, name: str) -> None:
        """Pick an image for this mouse and copy it into place."""
        from pathlib import Path
        from tkinter import filedialog, messagebox

        chosen = filedialog.askopenfilename(
            title=f"Choose an image for {name}",
            parent=self._root,
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return
        target = install_image(self.images_dir, key, name, Path(chosen))
        if target is None:
            messagebox.showerror(
                "Mouse Battery Tracker",
                "That file could not be read as an image.",
                parent=self._root,
            )
            return
        self._rebuild()

    def _open_images_dir(self) -> None:
        """Open the folder where per-device images go, creating it if needed."""
        import os
        import subprocess

        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(self.images_dir)  # noqa: S606  (Windows shell open)
        except AttributeError:
            subprocess.Popen(["xdg-open", str(self.images_dir)])
        except OSError:
            pass

    def _show_now(self) -> None:
        root = self._root
        if root is None:
            return
        self._rebuild()
        root.deiconify()
        root.lift()
        # Momentary topmost pulls the window in front of whatever has focus
        # without permanently pinning it above everything else.
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ---- rendering -------------------------------------------------------

    def _rebuild(self) -> None:
        tk = self._tk
        body = self._body
        if body is None:
            return

        for child in body.winfo_children():
            child.destroy()
        self._images = []

        online_keys = {key for key, _, _ in self._online}

        self._heading(body, "Connected")
        if self._online:
            for key, label, reading in self._online:
                detail = reading.describe()
                percent = reading.percent
                if percent is None:
                    # Charging reports no level, so show the last one we saw
                    # rather than an empty gauge -- clearly labelled as stale.
                    record = self.store.records.get(key)
                    if record is not None and record.percent is not None:
                        percent = record.percent
                        detail += f" · last known {record.percent}%"
                if reading.connection:
                    detail += f" · {reading.connection}"
                self._card(
                    body,
                    key=key,
                    name=self.store.display_name(key, label),
                    percent=percent,
                    charging=bool(reading.charging),
                    online=True,
                    detail=detail,
                )
        else:
            self._empty(body, "No mouse detected")

        recent = [r for r in self.store.recent(exclude=online_keys) if r.last_online]
        if recent:
            self._heading(body, "Recently used", pad_top=16)
            now = time.time()
            for record in recent[:MAX_RECENT]:
                self._card(
                    body,
                    key=record.key,
                    name=self.store.display_name(record.key, record.label),
                    percent=record.percent,
                    charging=False,
                    online=False,
                    detail=(
                        f"{record.describe_last_known()} · "
                        f"{format_age(now - record.last_online)}"
                    ),
                )

        footer = tk.Frame(body, bg=HUD_BG)
        footer.pack(fill="x", pady=(self.px(16), 0))
        tk.Label(
            footer,
            text="Esc or close to hide · the app keeps running in the tray",
            bg=HUD_BG,
            fg=HUD_MUTED,
            font=("Segoe UI", 8),
        ).pack(side="left")

        images = tk.Label(
            footer,
            text="image folder",
            bg=HUD_BG,
            fg=HUD_MUTED,
            font=("Segoe UI", 8, "underline"),
            cursor="hand2",
        )
        images.pack(side="right")
        images.bind("<Button-1>", lambda _e: self._open_images_dir())

    def _heading(self, parent, text: str, pad_top: int = 0) -> None:
        tk = self._tk
        tk.Label(
            parent,
            text=text.upper(),
            bg=HUD_BG,
            fg=HUD_MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(self.px(pad_top), self.px(6)))

    def _empty(self, parent, text: str) -> None:
        tk = self._tk
        frame = tk.Frame(parent, bg=HUD_CARD_DIM)
        frame.pack(fill="x", pady=self.px(3))
        tk.Label(
            frame, text=text, bg=HUD_CARD_DIM, fg=HUD_MUTED, font=("Segoe UI", 10)
        ).pack(anchor="w", padx=self.px(14), pady=self.px(14))

    def _card(
        self,
        parent,
        *,
        key: str,
        name: str,
        percent: int | None,
        charging: bool,
        online: bool,
        detail: str,
    ) -> None:
        from PIL import ImageTk

        tk = self._tk
        background = HUD_CARD if online else HUD_CARD_DIM

        card = tk.Frame(parent, bg=background)
        card.pack(fill="x", pady=self.px(3))

        art_size = self.px(ART_SIZE)
        art = load_custom(self.images_dir, key, art_size, name) or render_mouse(
            size=art_size, percent=percent, charging=charging, online=online
        )
        photo = ImageTk.PhotoImage(art)
        self._images.append(photo)
        art_label = tk.Label(card, image=photo, bg=background, cursor="hand2")
        art_label.pack(
            side="left", padx=(self.px(12), self.px(14)), pady=self.px(12)
        )
        art_label.bind(
            "<Button-1>", lambda _e, k=key, n=name: self._choose_image(k, n)
        )

        right = tk.Frame(card, bg=background)
        right.pack(
            side="left",
            fill="both",
            expand=True,
            pady=self.px(12),
            padx=(0, self.px(12)),
        )

        name_row = tk.Frame(right, bg=background)
        name_row.pack(fill="x")
        tk.Label(
            name_row,
            text=name,
            bg=background,
            fg=HUD_TEXT if online else HUD_MUTED,
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side="left")

        rename = tk.Label(
            name_row,
            text="rename",
            bg=background,
            fg=HUD_MUTED,
            font=("Segoe UI", 8, "underline"),
            cursor="hand2",
        )
        rename.pack(side="right", padx=(self.px(8), 0))
        rename.bind("<Button-1>", lambda _e, k=key, n=name: self._rename(k, n))

        accent = to_hex(level_color(percent, charging)) if online else HUD_MUTED
        tk.Label(
            right,
            text=detail,
            bg=background,
            fg=accent,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(self.px(1), self.px(7)))

        self._bar(right, percent, accent, background)

    def _bar(self, parent, percent: int | None, accent: str, background: str) -> None:
        """Battery bar drawn on a Canvas -- ttk.Progressbar can't be recoloured
        reliably across Windows themes."""
        tk = self._tk
        height = self.px(7)
        bar = tk.Canvas(
            parent,
            height=height,
            bg=background,
            highlightthickness=0,
            bd=0,
        )
        bar.pack(fill="x")

        def draw(_event=None) -> None:
            bar.delete("all")
            width = bar.winfo_width()
            if width <= 1:
                return
            bar.create_rectangle(0, 0, width, height, fill=HUD_TRACK, outline="")
            if percent is not None:
                filled = width * max(0, min(100, percent)) / 100
                if filled > 0:
                    bar.create_rectangle(0, 0, filled, height, fill=accent, outline="")

        bar.bind("<Configure>", draw)
