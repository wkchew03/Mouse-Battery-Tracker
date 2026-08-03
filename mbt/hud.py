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
from .cards import render_card, render_ring, status_dot
from .drivers.base import Reading
from .mouseart import install_image, load_custom, render_mouse
from .store import Store, format_age
from .theme import (
    COLOR_HIGH,
    HUD_BG,
    HUD_CARD,
    HUD_CARD_DIM,
    HUD_HERO,
    HUD_MUTED,
    HUD_TEXT,
    HUD_TRACK,
    dim,
    level_color,
    to_hex,
)

HERO_ART = 96
GRID_ART = 46
RING_SIZE = 92
WINDOW_WIDTH = 540
MAX_RECENT = 8
GRID_COLUMNS = 2


class Hud:
    """Owns the Tk thread. Safe to call show()/update()/stop() from anywhere."""

    def __init__(self, store: Store, images_dir: Path | None = None) -> None:
        self.store = store
        self.images_dir = images_dir or (store.directory / "images")
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._body = None
        self._canvas = None
        self._scrollbar = None
        self._window = None
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

        # Scrollable body: a Canvas that scrolls an inner Frame. Tk has no
        # scrollable container, so the cards live in a Frame embedded in a
        # Canvas window, and the canvas scrolls that.
        outer = tk.Frame(root, bg=HUD_BG)
        outer.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(14))

        self._canvas = tk.Canvas(
            outer, bg=HUD_BG, highlightthickness=0, bd=0, takefocus=0
        )
        self._scrollbar = tk.Scrollbar(
            outer, orient="vertical", command=self._canvas.yview
        )
        self._canvas.configure(yscrollcommand=self._on_scroll_range)
        self._canvas.pack(side="left", fill="both", expand=True)

        self._body = tk.Frame(self._canvas, bg=HUD_BG)
        self._window = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw"
        )

        # Keep the inner frame exactly as wide as the canvas, or cards would
        # size to their content and the layout would jump around.
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._window, width=e.width),
        )
        self._body.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")
            ),
        )

        self._bind_wheel(root)

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

    # ---- scrolling -------------------------------------------------------

    def _on_scroll_range(self, first: str, last: str) -> None:
        """Show the scrollbar only when the content actually overflows."""
        if self._scrollbar is None:
            return
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._scrollbar.pack_forget()
        else:
            self._scrollbar.pack(side="right", fill="y")
        self._scrollbar.set(first, last)

    def _bind_wheel(self, root) -> None:
        """Wheel scrolling anywhere in the window.

        bind_all rather than binding the canvas: the cards sit on top of it, so
        a wheel event over a card never reaches the canvas otherwise.
        """
        root.bind_all("<MouseWheel>", self._on_wheel)
        root.bind_all("<Button-4>", self._on_wheel)  # X11
        root.bind_all("<Button-5>", self._on_wheel)
        root.bind("<Prior>", lambda _e: self._scroll_page(-1))
        root.bind("<Next>", lambda _e: self._scroll_page(1))
        root.bind("<Home>", lambda _e: self._canvas.yview_moveto(0.0))
        root.bind("<End>", lambda _e: self._canvas.yview_moveto(1.0))

    def _scrollable(self) -> bool:
        if self._canvas is None:
            return False
        first, last = self._canvas.yview()
        return not (first <= 0.0 and last >= 1.0)

    def _on_wheel(self, event) -> None:
        if not self._scrollable():
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            # Windows reports multiples of 120.
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta * 2, "units")

    def _scroll_page(self, direction: int) -> None:
        if self._scrollable():
            self._canvas.yview_scroll(direction, "pages")

    # ---- window ----------------------------------------------------------

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
        if self._canvas is not None:
            self._canvas.yview_moveto(0.0)

        online_keys = {key for key, _, _ in self._online}

        self._heading(body, "Currently connected")
        if self._online:
            for key, label, reading in self._online:
                self._hero_card(body, key, label, reading)
        else:
            self._empty(body, "No mouse detected")

        recent = [r for r in self.store.recent(exclude=online_keys) if r.last_online]
        if recent:
            self._heading(body, "Device history", pad_top=18)
            self._history_grid(body, recent[:MAX_RECENT])

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

    def _rounded(self, parent, height: int, fill: str) -> tuple:
        """A frame with a rounded background image behind its children.

        Tkinter cannot round a Frame, so the corners come from an image placed
        behind the content. Children use the same solid fill, so the rounding is
        only visible where it matters -- at the corners.

        The background is re-rendered whenever the frame is resized. Drawing it
        once at a size derived from a constant looked correct only at the
        default window width: any wider and the card stopped short while its
        contents carried on past the edge.
        """
        from PIL import ImageTk

        tk = self._tk
        holder = tk.Frame(parent, bg=HUD_BG, height=height)
        backdrop = tk.Label(holder, bg=HUD_BG, bd=0)
        backdrop.place(x=0, y=0, relwidth=1, relheight=1)

        last = {"size": None}

        def redraw(_event=None):
            width = holder.winfo_width()
            actual = holder.winfo_height()
            if width <= 1 or actual <= 1:
                return
            # Quantise so dragging the window does not render a new bitmap for
            # every single pixel of width.
            width = max(8, (width // 4) * 4)
            if last["size"] == (width, actual):
                return
            last["size"] = (width, actual)
            photo = ImageTk.PhotoImage(
                render_card(width, actual, radius=self.px(14), fill=fill)
            )
            backdrop.configure(image=photo)
            # Held on the widget so it survives GC without growing the shared
            # image list every time the window is resized.
            backdrop.image = photo

        holder.bind("<Configure>", redraw)
        return holder, backdrop

    def _hero_card(self, parent, key: str, label: str, reading: Reading) -> None:
        """Large card for the connected mouse: artwork, status, ring gauge."""
        from PIL import ImageTk

        tk = self._tk
        name = self.store.display_name(key, label)

        percent = reading.percent
        stale_note = ""
        if percent is None:
            # Charging often reports no level; fall back to the stored one and
            # say so rather than showing an empty ring.
            record = self.store.records.get(key)
            if record is not None and record.percent is not None:
                percent = record.percent
                stale_note = f"last known {record.percent}%"

        card, _ = self._rounded(parent, self.px(126), HUD_HERO)
        card.pack(fill="x", pady=self.px(4))
        card.pack_propagate(False)

        # Artwork
        art = load_custom(
            self.images_dir, key, self.px(HERO_ART), name
        ) or render_mouse(
            size=self.px(HERO_ART),
            percent=percent,
            charging=bool(reading.charging),
            online=True,
        )
        art_photo = ImageTk.PhotoImage(art)
        self._images.append(art_photo)
        art_label = tk.Label(card, image=art_photo, bg=HUD_HERO, cursor="hand2", bd=0)
        art_label.pack(side="left", padx=(self.px(16), self.px(14)), pady=self.px(14))
        art_label.bind(
            "<Button-1>", lambda _e, k=key, n=name: self._choose_image(k, n)
        )

        # Ring gauge on the right
        ring = ImageTk.PhotoImage(
            render_ring(
                self.px(RING_SIZE),
                percent,
                charging=bool(reading.charging),
                online=True,
            )
        )
        self._images.append(ring)
        tk.Label(card, image=ring, bg=HUD_HERO, bd=0).pack(
            side="right", padx=(self.px(10), self.px(18))
        )

        # Text column
        text = tk.Frame(card, bg=HUD_HERO)
        text.pack(side="left", fill="both", expand=True, pady=self.px(16))

        name_row = tk.Frame(text, bg=HUD_HERO)
        name_row.pack(fill="x")
        tk.Label(
            name_row,
            text=name,
            bg=HUD_HERO,
            fg=HUD_TEXT,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(side="left")
        rename = tk.Label(
            name_row,
            text="rename",
            bg=HUD_HERO,
            fg=HUD_MUTED,
            font=("Segoe UI", 8, "underline"),
            cursor="hand2",
        )
        rename.pack(side="left", padx=(self.px(8), 0))
        rename.bind("<Button-1>", lambda _e, k=key, n=name: self._rename(k, n))

        # Status line with a live dot
        status = tk.Frame(text, bg=HUD_HERO)
        status.pack(fill="x", pady=(self.px(6), 0))
        dot = ImageTk.PhotoImage(status_dot(self.px(8), COLOR_HIGH))
        self._images.append(dot)
        tk.Label(status, image=dot, bg=HUD_HERO, bd=0).pack(side="left")

        bits = ["Active"]
        if reading.connection:
            bits.append(reading.connection)
        if reading.charging:
            bits.append("charging")
        tk.Label(
            status,
            text="  " + " · ".join(bits),
            bg=HUD_HERO,
            fg=HUD_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

        remaining = (
            f"{percent}% remaining" if percent is not None else reading.describe()
        )
        tk.Label(
            text,
            text=remaining,
            bg=HUD_HERO,
            fg=to_hex(level_color(percent, bool(reading.charging))),
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x", pady=(self.px(4), 0))

        if stale_note:
            tk.Label(
                text,
                text=stale_note,
                bg=HUD_HERO,
                fg=HUD_MUTED,
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(fill="x")

    def _history_grid(self, parent, records) -> None:
        """Two-column grid of previously seen mice."""
        tk = self._tk
        grid = tk.Frame(parent, bg=HUD_BG)
        grid.pack(fill="x")
        for column in range(GRID_COLUMNS):
            grid.grid_columnconfigure(column, weight=1, uniform="cards")

        now = time.time()
        gap = self.px(4)
        # Tall enough for a two-line name; the card fills its grid cell
        # horizontally, so no width is fixed here.
        height = self.px(104)

        for index, record in enumerate(records):
            holder, _ = self._rounded(grid, height, HUD_CARD)
            holder.grid(
                row=index // GRID_COLUMNS,
                column=index % GRID_COLUMNS,
                padx=gap,
                pady=gap,
                sticky="ew",
            )
            holder.grid_propagate(False)
            self._history_card(holder, record, now)

    def _history_card(self, card, record, now: float) -> None:
        from PIL import ImageTk

        tk = self._tk
        name = self.store.display_name(record.key, record.label)

        top = tk.Frame(card, bg=HUD_CARD)
        top.pack(fill="x", padx=self.px(12), pady=(self.px(11), 0))

        art = load_custom(
            self.images_dir, record.key, self.px(GRID_ART), name
        ) or render_mouse(
            size=self.px(GRID_ART), percent=record.percent, online=False
        )
        photo = ImageTk.PhotoImage(art)
        self._images.append(photo)
        art_label = tk.Label(top, image=photo, bg=HUD_CARD, cursor="hand2", bd=0)
        art_label.pack(side="left", padx=(0, self.px(9)))
        art_label.bind(
            "<Button-1>",
            lambda _e, k=record.key, n=name: self._choose_image(k, n),
        )

        right = tk.Frame(top, bg=HUD_CARD)
        right.pack(side="left", fill="both", expand=True)

        label = tk.Label(
            right,
            text=name,
            bg=HUD_CARD,
            fg=HUD_TEXT,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=self.px(150),
        )
        label.pack(fill="x")
        label.bind("<Button-1>", lambda _e, k=record.key, n=name: self._rename(k, n))
        label.configure(cursor="hand2")

        # Wrap to the space actually available rather than a fixed width, so a
        # wider window gives long names more room instead of wrapping early.
        def rewrap(_event=None, widget=right, target=label):
            available = widget.winfo_width()
            if available > 1:
                target.configure(wraplength=max(self.px(80), available - self.px(6)))

        right.bind("<Configure>", rewrap)

        # Dimmed: these readings are not live, and at full brightness they were
        # indistinguishable from the connected mouse's.
        stale_colour = to_hex(dim(level_color(record.percent)))

        tk.Label(
            right,
            text=record.describe_last_known(),
            bg=HUD_CARD,
            fg=stale_colour,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x")

        bar_row = tk.Frame(card, bg=HUD_CARD)
        bar_row.pack(fill="x", padx=self.px(12), pady=(self.px(6), 0))
        self._bar(bar_row, record.percent, stale_colour, HUD_CARD)

        tk.Label(
            card,
            text=f"Last active: {format_age(now - record.last_online)}",
            bg=HUD_CARD,
            fg=HUD_MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=self.px(12), pady=(self.px(5), self.px(10)))


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
