"""Detail window: the connected mouse pinned left, everything else a shelf.

Threading is unchanged from the widget version: pystray owns the main thread
on Windows, so Tk runs on its own thread and owns everything it creates. Tk is
not thread-safe, so the tray thread never touches a widget -- it posts messages
onto a queue which the Tk thread drains from inside its own event loop.

What changed is how the window is *painted*. The design is frosted panels over
a slowly drifting colour field, and Tk has neither alpha compositing on widgets
nor a blur. So the window is one Canvas:

- the field is a single oversized image, panned (see prism.py);
- every panel and card is an RGBA sprite -- a Tk photo image blends against
  whatever canvas item is beneath it, which is what makes the frosting real
  rather than a flat colour picked to look like it;
- all text is `create_text`, which has no opaque background and renders with
  the system's own font engine, so it stays as sharp as a Label would be.

The scroll bar is gone with the widget tree: the shelf scrolls by wheel and
the fades top and bottom are what say there is more of it.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import autostart, dpi, history, prism
from .cards import fade_mask, render_discharge_chart, render_frost, render_icon
from .drivers.base import Reading
from .mouseart import install_image, load_custom, render_mouse
from .store import (
    MAX_ALERT_THRESHOLD,
    MIN_ALERT_THRESHOLD,
    Store,
    format_age,
)
from .theme import (
    PRISM_CARD,
    PRISM_CARD_EDGE,
    PRISM_CARD_EDGE_HOVER,
    PRISM_CARD_HOVER,
    PRISM_GROUND,
    PRISM_HAIRLINE,
    PRISM_PANEL,
    PRISM_OVERLAY,
    PRISM_PANEL_EDGE,
    PRISM_SCRIM,
    PRISM_TEXT,
    PRISM_TEXT_DIM,
    PRISM_TEXT_FAINT,
    PRISM_TEXT_SOFT,
    level_color,
    to_hex,
)

WINDOW_WIDTH = 900
WINDOW_HEIGHT = 600
PAD = 16

PANEL_WIDTH = 320
# The connected mouse's picture is PANEL_ART_MAX where there is room, and
# otherwise whatever height the panel has left after the text and chart below
# it (PANEL_BELOW_ART), so a shorter window shrinks it instead of overlapping.
# 210 was chosen by eye: 120 read as too small, filling half the panel (264)
# as too large.
PANEL_ART_TOP = 24
PANEL_BELOW_ART = 280
PANEL_ART_MIN = 80
PANEL_ART_MAX = 210
CHART_HEIGHT = 92

CARD_HEIGHT = 126
CARD_ART = 56
CARD_GAP = 10
GRID_COLUMNS = 3

FADE_HEIGHT = 64
TOP_FADE_HEIGHT = 40

# The fixed row above the shelf: the mouse count and "Add mouse", which must
# never scroll out of reach.
SHELF_HEADER = 40
PILL_HEIGHT = 28

# The "Add mouse" overlay's card, at 100% scale.
OVERLAY_WIDTH = 460

# One pan step. Slow enough to read as drift rather than motion, cheap enough
# that it is a coords() call and nothing else.
TICK_MS = 70

# How often the tick wakes while the window is hidden, only to notice that it
# still is.
IDLE_TICK_MS = 700


class Hud:
    """Owns the Tk thread. Safe to call show()/update()/stop() from anywhere."""

    def __init__(
        self,
        store: Store,
        images_dir: Path | None = None,
        on_refresh=None,
    ) -> None:
        self.store = store
        self.images_dir = images_dir or (store.directory / "images")
        # Called when the window is opened, to ask for a fresh poll. Without it
        # the HUD shows whatever the last scheduled poll found, which can be a
        # full interval old -- so a mouse connected moments ago looks absent.
        self.on_refresh = on_refresh
        # Runs a job on the poll thread, which owns HID; set by run_tray. The
        # "Add mouse" scan goes through it because this thread never opens a
        # device.
        self.run_on_poll_thread = None
        # The "Add mouse" overlay: which step it shows and that step's data,
        # or None while it is closed. See _paint_overlay.
        self._overlay: dict | None = None
        # Bumped whenever the overlay moves on, so a scan that finishes after
        # the user closed it or went elsewhere is dropped instead of shown.
        self._overlay_seq = 0
        self._last_results: dict | None = None
        self._overlay_sprites: list = []
        # (activate, focus ring item) per overlay button, in Tab order.
        self._overlay_buttons: list = []
        self._overlay_focus = 0
        # The focus ring only appears once the keyboard is in use.
        self._overlay_keyboard = False
        self._overlay_entry = None
        self._overlay_dots = None
        self._dots_running = False
        self._dots_count = 0
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._canvas = None
        self._bg_item = None
        self._field: prism.Field | None = None
        # Tk garbage-collects images that nothing references, leaving blank
        # items, so every sprite in the current view is kept alive here.
        self._sprites: list = []
        self._online: list[tuple[str, str, Reading]] = []
        self._last_sync = 0.0
        self._scroll = 0.0
        self._scroll_limit = 0.0
        self._fade_top: int | None = None
        self._fade_bottom: int | None = None
        # (item, photo, (x, y, w, h), mask) per fade, repainted as the field pans.
        self._fades: list = []
        self._fade_offset: tuple[int, int] | None = None
        self._hover_card: int | None = None
        self._chart_hover: int | None = None
        self._chart_box: tuple[int, int, int, int] | None = None
        self._chart_points: list[tuple[float, int]] = []
        self._chart_caption_text = ""
        # index -> (canvas item, width, height), so a hover repaints one card
        # instead of the whole window.
        self._card_items: dict[int, tuple[int, int, int]] = {}
        self._chart_item: int | None = None
        self._chart_caption: int | None = None
        self._chart_geometry: tuple[int, int, str] | None = None
        self._started = 0.0
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
        root.configure(bg=PRISM_GROUND)

        self.scale = dpi.scale()
        # Point-sized fonts render at the correct physical size only if Tk knows
        # how many pixels there are per point.
        root.tk.call("tk", "scaling", dpi.tk_scaling())

        width, height = self.px(WINDOW_WIDTH), self.px(WINDOW_HEIGHT)
        root.geometry(f"{width}x{height}")
        root.minsize(self.px(PANEL_WIDTH + 300), self.px(420))

        # Closing the window hides it; the app keeps running in the tray.
        root.protocol("WM_DELETE_WINDOW", self._hide)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Tab>", lambda _e: self._overlay_tab(1))
        root.bind("<Shift-Tab>", lambda _e: self._overlay_tab(-1))
        root.bind("<Return>", self._overlay_activate)

        self._canvas = tk.Canvas(
            root, highlightthickness=0, bd=0, takefocus=0, bg=PRISM_GROUND
        )
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_resize)

        self._bind_wheel(root)

        # Create it up front so the folder is there to drop images into,
        # rather than only appearing once someone clicks the link.
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self._started = time.time()
        root.withdraw()
        root.after(120, self._drain)
        root.after(TICK_MS, self._tick)
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
                    # Stamped here rather than in the tray so "last sync" means
                    # "when this window last heard", which is what it claims.
                    self._last_sync = time.time()
                    if self._root is not None and self._root.state() != "withdrawn":
                        self._rebuild()
                elif kind == "overlay":
                    seq, state = message[1]
                    if self._overlay is not None and seq == self._overlay_seq:
                        self._set_overlay(**state)
                elif kind == "quit":
                    if self._root is not None:
                        self._root.quit()
                        self._root.destroy()
                    return
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(150, self._drain)

    # ---- the drifting field ---------------------------------------------

    def _tick(self) -> None:
        """Pan the field one step. Runs only while the window is on screen."""
        root, canvas = self._root, self._canvas
        if root is None or canvas is None:
            return
        try:
            visible = root.state() != "withdrawn"
        except Exception:
            visible = False
        if visible and self._field is not None and self._bg_item is not None:
            x, y = self._field.offset(time.time() - self._started)
            canvas.coords(self._bg_item, x, y)
            if (x, y) != self._fade_offset:
                self._paint_fades()
        # The window is withdrawn most of the time. Idling at the animation
        # rate would keep a timer firing fourteen times a second for a window
        # nobody is looking at, which is the opposite of what this app claims.
        root.after(TICK_MS if visible else IDLE_TICK_MS, self._tick)

    def _ensure_field(self, width: int, height: int) -> None:
        """Render the field, but only when its size actually changed."""
        from PIL import ImageTk

        if self._field is not None and self._field.matches(width, height):
            return
        self._field = prism.Field(width, height)
        self._field_photo = ImageTk.PhotoImage(self._field.image)
        if self._bg_item is None:
            self._bg_item = self._canvas.create_image(
                0, 0, image=self._field_photo, anchor="nw", tags=("field",)
            )
        else:
            self._canvas.itemconfigure(self._bg_item, image=self._field_photo)
        self._canvas.tag_lower(self._bg_item)

    def _on_resize(self, _event=None) -> None:
        if self._root is not None and self._root.state() != "withdrawn":
            self._rebuild()

    # ---- scrolling -------------------------------------------------------

    def _bind_wheel(self, root) -> None:
        root.bind_all("<MouseWheel>", self._on_wheel)
        root.bind_all("<Button-4>", self._on_wheel)  # X11
        root.bind_all("<Button-5>", self._on_wheel)
        root.bind("<Prior>", lambda _e: self._scroll_by(-self.px(200)))
        root.bind("<Next>", lambda _e: self._scroll_by(self.px(200)))
        root.bind("<Home>", lambda _e: self._scroll_by(-self._scroll))
        root.bind("<End>", lambda _e: self._scroll_by(self._scroll_limit))

    def _on_wheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            # Windows reports multiples of 120.
            delta = -1 if event.delta > 0 else 1
        self._scroll_by(delta * self.px(56))

    def _scroll_by(self, amount: float) -> None:
        canvas = self._canvas
        if canvas is None or self._scroll_limit <= 0 or self._overlay is not None:
            return
        target = max(0.0, min(self._scroll_limit, self._scroll + amount))
        moved = target - self._scroll
        if not moved:
            return
        self._scroll = target
        canvas.move("shelf", 0, -moved)
        self._update_fades()

    def _update_fades(self) -> None:
        """Each fade says there is more that way, so it goes once there isn't.

        Scrolling only moves the cards, so this has to follow every scroll
        rather than being decided once at paint time -- that left the bottom
        fade hiding the last row after scrolling all the way down.
        """
        canvas = self._canvas
        for item, shown in (
            (self._fade_top, self._scroll > 0),
            (self._fade_bottom, self._scroll < self._scroll_limit),
        ):
            if item is not None:
                canvas.itemconfigure(item, state="normal" if shown else "hidden")
        # A fade that was hidden missed the field's drift while it was.
        self._paint_fades()

    def _paint_fades(self) -> None:
        """Paint each fade from the colour field under it.

        A fade to a flat colour either stays translucent, which leaves the
        cards showing through, or goes solid and draws a dark bar across the
        drifting field. Cut from the field itself, the solid end is the
        background, so the cards dissolve into it. Only the fades' strips are
        re-cut (~0.5 ms), and only on ticks where the field actually moved.
        """
        canvas, field = self._canvas, self._field
        if canvas is None or field is None or self._bg_item is None:
            return
        ox, oy = (int(v) for v in canvas.coords(self._bg_item))
        self._fade_offset = (ox, oy)
        for item, photo, (fx, fy, fw, fh), mask in self._fades:
            if canvas.itemcget(item, "state") == "hidden":
                continue
            strip = field.image.crop((fx - ox, fy - oy, fx - ox + fw, fy - oy + fh))
            strip = strip.convert("RGBA")
            strip.putalpha(mask)
            photo.paste(strip)

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
        # Already open: just bring it forward. pystray fires the default action
        # on every click, so a double-click on the tray icon arrives as two
        # shows, and each used to cost a device poll and a full rebuild.
        if root.state() in ("normal", "zoomed"):
            root.lift()
            root.focus_force()
            return
        # Ask for a fresh reading as the window opens; the result arrives via
        # the queue and triggers another rebuild.
        if self.on_refresh is not None:
            try:
                self.on_refresh("hud-open")
            except TypeError:
                self.on_refresh()
            except Exception:
                pass
        self._rebuild()
        root.deiconify()
        root.lift()
        # Momentary topmost pulls the window in front of whatever has focus
        # without permanently pinning it above everything else.
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ---- painting --------------------------------------------------------

    def _rebuild(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return

        # An unmapped canvas reports 1, not 0, so `or` is not enough of a
        # guard: the first paint would size the panel to a negative height.
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < self.px(200) or height < self.px(200):
            width, height = self.px(WINDOW_WIDTH), self.px(WINDOW_HEIGHT)

        # Only the painted layer is torn down. The field is independent of
        # layout and survives every repaint -- rebuilding it here would throw
        # away the pan position and re-render it on every hover.
        canvas.delete("paint")
        # The item under the pointer may have just been deleted without a
        # <Leave>, which would leave the hand cursor stuck.
        canvas.configure(cursor="")
        self._sprites = []
        self._ensure_field(width, height)

        pad = self.px(PAD)
        panel_w = self.px(PANEL_WIDTH)
        self._paint_panel(pad, pad, panel_w, height - pad * 2)

        shelf_x = pad + panel_w + pad
        self._paint_shelf(shelf_x, pad, width - shelf_x - pad, height - pad * 2)
        # Last, so it covers everything the rebuild just painted.
        self._paint_overlay()

    def _sprite(self, image):
        from PIL import ImageTk

        photo = ImageTk.PhotoImage(image)
        self._sprites.append(photo)
        return photo

    def _text(self, x, y, text, *, fill, size, bold=False, anchor="nw",
              family="Segoe UI", tags=("paint",)):
        font = (family, size, "bold") if bold else (family, size)
        return self._canvas.create_text(
            x, y, text=text, fill=fill, font=font, anchor=anchor, tags=tags
        )

    def _clickable(self, item, action, hint: str) -> None:
        """Hand cursor and a hint on hover, so a click target says it is one.

        Nothing else in the design marks these: a name that renames on click
        looks exactly like a label.
        """
        canvas = self._canvas

        def enter(event):
            canvas.configure(cursor="hand2")
            self._show_tip(event.x, event.y, hint)

        def leave(_event):
            canvas.configure(cursor="")
            canvas.delete("tip")

        def click(_event):
            leave(None)
            action()

        canvas.tag_bind(item, "<Enter>", enter, add="+")
        canvas.tag_bind(item, "<Leave>", leave, add="+")
        canvas.tag_bind(item, "<Button-1>", click)

    def _show_tip(self, x: int, y: int, text: str) -> None:
        canvas = self._canvas
        canvas.delete("tip")
        pad = self.px(5)
        label = self._text(x + self.px(12), y + self.px(18), text,
                           fill=PRISM_TEXT_SOFT, size=8, tags=("paint", "tip"))
        left, top, right, bottom = canvas.bbox(label)
        # Keep it inside the window: the gear sits against the panel's edge,
        # and the rightmost cards against the window's.
        overflow = right + pad - (canvas.winfo_width() - self.px(4))
        if overflow > 0:
            canvas.move(label, -overflow, 0)
            left, right = left - overflow, right - overflow
        box = canvas.create_rectangle(
            left - pad, top - pad // 2, right + pad, bottom + pad // 2,
            fill=PRISM_HAIRLINE, outline=PRISM_TEXT_FAINT, tags=("paint", "tip"),
        )
        canvas.tag_raise(label, box)

    # ---- the pinned panel ------------------------------------------------

    def _paint_panel(self, x: int, y: int, w: int, h: int) -> None:
        canvas = self._canvas
        frost = self._sprite(
            render_frost(w, h, self.px(18), PRISM_PANEL, PRISM_PANEL_EDGE)
        )
        canvas.create_image(x, y, image=frost, anchor="nw", tags=("paint",))

        self._paint_panel_actions(x + w - self.px(22), y + self.px(16))

        if not self._online:
            self._text(
                x + w // 2, y + h // 2, "No mouse detected",
                fill=PRISM_TEXT_FAINT, size=10, anchor="center",
            )
            return

        key, label, reading = self._online[0]
        name = self.store.display_name(key, label)
        record = self.store.records.get(key)

        percent = reading.percent
        if percent is None and record is not None:
            percent = record.percent
        colour = to_hex(level_color(percent, bool(reading.charging)))

        art_size = max(
            self.px(PANEL_ART_MIN),
            min(self.px(PANEL_ART_MAX), w - self.px(40),
                h - self.px(PANEL_ART_TOP + PANEL_BELOW_ART)),
        )
        art = load_custom(self.images_dir, key, art_size, name) or render_mouse(
            size=art_size, percent=percent, charging=bool(reading.charging), online=True
        )
        art_photo = self._sprite(art)
        art_item = canvas.create_image(
            x + w // 2, y + self.px(PANEL_ART_TOP), image=art_photo, anchor="n",
            tags=("paint",)
        )
        self._clickable(art_item, lambda k=key, n=name: self._choose_image(k, n),
                        "Choose a picture")

        top = y + self.px(PANEL_ART_TOP) + art_size

        # The number and its sign are separate items so they can be sized apart.
        shown = "--" if percent is None else str(percent)
        number = self._text(x + w // 2, top + self.px(14), shown,
                            fill=PRISM_TEXT, size=40, anchor="n",
                            family="Segoe UI Light")
        bounds = canvas.bbox(number)
        if bounds and percent is not None:
            self._text(bounds[2] + self.px(2), bounds[3] - self.px(14), "%",
                       fill=PRISM_TEXT_DIM, size=17, anchor="sw",
                       family="Segoe UI Light")

        name_y = top + self.px(74)
        name_item = self._text(x + w // 2, name_y, name, fill=PRISM_TEXT_SOFT,
                               size=9, anchor="n")
        self._clickable(name_item, lambda k=key, n=name: self._rename(k, n),
                        "Rename")

        bits = ["connected now" if reading.online else "off"]
        if reading.connection:
            bits.append(reading.connection)
        if reading.charging:
            bits.append("charging")
        dot = self.px(3)
        centre = x + w // 2
        status = " · ".join(bits)
        status_y = name_y + self.px(22)
        text_item = self._text(centre + dot, status_y, status,
                               fill=PRISM_TEXT_DIM, size=8, anchor="n")
        bounds = canvas.bbox(text_item)
        if bounds:
            canvas.coords(text_item, centre + dot * 3, status_y)
            bounds = canvas.bbox(text_item)
            cy = (bounds[1] + bounds[3]) / 2
            canvas.create_oval(
                bounds[0] - dot * 4, cy - dot, bounds[0] - dot * 2, cy + dot,
                fill=colour, outline="", tags=("paint",),
            )

        self._paint_chart(x, y, w, h, key, percent, colour, record)

    def _paint_panel_actions(self, x: int, y: int) -> None:
        """A gear and a folder, in the panel's top corner.

        The approved design has no settings block -- so the controls that used
        to live in one move behind these rather than disappearing with it.
        """
        from PIL import Image

        canvas = self._canvas
        size = self.px(16)
        # Tk hit-tests an image item by its box, so padding the glyph onto a
        # larger transparent square is what makes it easy to click.
        hit = self.px(28)
        inset = (hit - size) // 2
        for offset, name, action, hint in (
            (0, "gear", self._open_settings, "Settings"),
            (hit, "folder", self._open_images_dir, "Open pictures folder"),
        ):
            padded = Image.new("RGBA", (hit, hit), (0, 0, 0, 0))
            padded.alpha_composite(render_icon(name, size, PRISM_TEXT_DIM),
                                   (inset, inset))
            photo = self._sprite(padded)
            item = canvas.create_image(x + inset - offset, y - inset, image=photo,
                                       anchor="ne", tags=("paint",))
            self._clickable(item, action, hint)

    def _paint_chart(self, x, y, w, h, key, percent, colour, record) -> None:
        """The current discharge, with whatever estimate the data supports."""
        samples = record.history if record is not None else []
        points = history.discharge_series(samples)
        caption = history.summary(samples, percent) or "not enough data yet"
        self._chart_caption_text = caption

        chart_w = w - self.px(40)
        chart_h = self.px(CHART_HEIGHT)
        chart_x = x + self.px(20)
        chart_y = y + h - self.px(18) - chart_h - self.px(16)

        self._chart_points = points
        self._chart_box = (chart_x, chart_y, chart_w, chart_h)

        image = render_discharge_chart(
            chart_w, chart_h, points, colour,
            threshold=self.store.alert_threshold,
            hover=self._chart_hover,
        )
        photo = self._sprite(image)
        item = self._canvas.create_image(chart_x, chart_y, image=photo,
                                         anchor="nw", tags=("paint",))
        if points:
            self._canvas.tag_bind(item, "<Motion>", self._on_chart_motion)
            self._canvas.tag_bind(item, "<Leave>", self._on_chart_leave)

        self._chart_item = item
        self._chart_geometry = (chart_w, chart_h, colour)
        self._chart_caption = self._text(
            chart_x + chart_w // 2, chart_y + chart_h + self.px(4),
            self._chart_detail(caption), fill=PRISM_TEXT_DIM, size=8, anchor="n",
        )

    def _chart_detail(self, caption: str) -> str:
        """The caption, or the point under the cursor while there is one."""
        points = self._chart_points
        at = self._chart_hover
        if at is None or at >= len(points):
            return caption
        seconds, percent = points[at]
        ago = points[-1][0] - seconds
        return f"{percent}% · {format_age(ago)}" if ago else f"{percent}% · now"

    def _repaint_chart(self) -> None:
        if self._chart_item is None or self._chart_geometry is None:
            return
        chart_w, chart_h, colour = self._chart_geometry
        photo = self._sprite(render_discharge_chart(
            chart_w, chart_h, self._chart_points, colour,
            threshold=self.store.alert_threshold, hover=self._chart_hover,
        ))
        self._canvas.itemconfigure(self._chart_item, image=photo)
        if self._chart_caption is not None:
            self._canvas.itemconfigure(
                self._chart_caption, text=self._chart_detail(self._chart_caption_text)
            )

    def _on_chart_motion(self, event) -> None:
        if not self._chart_points or self._chart_box is None:
            return
        chart_x, _, chart_w, _ = self._chart_box
        span = self._chart_points[-1][0] - self._chart_points[0][0] or 1.0
        fraction = max(0.0, min(1.0, (event.x - chart_x) / max(1, chart_w)))
        target = self._chart_points[0][0] + fraction * span
        nearest = min(
            range(len(self._chart_points)),
            key=lambda i: abs(self._chart_points[i][0] - target),
        )
        if nearest != self._chart_hover:
            self._chart_hover = nearest
            self._repaint_chart()

    def _on_chart_leave(self, _event) -> None:
        if self._chart_hover is not None:
            self._chart_hover = None
            self._repaint_chart()

    # ---- the shelf -------------------------------------------------------

    def _paint_shelf(self, x: int, y: int, w: int, h: int) -> None:
        canvas = self._canvas
        # Only the pinned mouse is left out. A second one online at once -- a
        # placeholder's receiver is online whenever it is plugged in -- belongs
        # on the shelf, not nowhere.
        pinned = {self._online[0][0]} if self._online else set()
        records = [r for r in self.store.recent(exclude=pinned) if r.last_online]

        gap = self.px(CARD_GAP)
        card_w = (w - gap * (GRID_COLUMNS - 1)) // GRID_COLUMNS
        card_h = self.px(CARD_HEIGHT)
        now = time.time()
        header = self.px(SHELF_HEADER)
        grid_y, grid_h = y + header, h - header

        self._card_items = {}
        for index, record in enumerate(records):
            column = index % GRID_COLUMNS
            row = index // GRID_COLUMNS
            cx = x + column * (card_w + gap)
            cy = grid_y + row * (card_h + gap) - self._scroll
            self._paint_card(index, record, cx, cy, card_w, card_h, now)

        rows = (len(records) + GRID_COLUMNS - 1) // GRID_COLUMNS
        content = rows * card_h + max(0, rows - 1) * gap
        self._scroll_limit = max(0.0, content - grid_h)

        # A resize can shrink the limit below where the shelf was scrolled to.
        if self._scroll > self._scroll_limit:
            canvas.move("shelf", 0, self._scroll - self._scroll_limit)
            self._scroll = self._scroll_limit

        # Drawn last so they sit over the cards. Both exist whenever the shelf
        # scrolls; _update_fades shows each only while there is more that way.
        # Each runs on to the window's edge: the cards carry on drawing into
        # the margin, and a fade that stopped at the shelf left them showing
        # there at full strength.
        self._fade_top = self._fade_bottom = None
        self._fades = []
        if self._scroll_limit > 0:
            ramp, top_ramp = self.px(FADE_HEIGHT), self.px(TOP_FADE_HEIGHT)
            self._fade_bottom = self._add_fade(
                x, y + h - ramp, w, ramp + y, fade_mask(w, ramp + y, ramp))
            # Solid down to the grid, so cards scrolled up pass under the
            # header rather than through it.
            self._fade_top = self._add_fade(
                x, 0, w, grid_y + top_ramp,
                fade_mask(w, grid_y + top_ramp, top_ramp, reverse=True))
        self._update_fades()

        # After the fades, so the header sits over the top one.
        self._paint_shelf_header(x, y, w, len(records) + len(pinned))

    def _add_fade(self, x: int, y: int, w: int, h: int, mask) -> int:
        from PIL import ImageTk

        photo = ImageTk.PhotoImage("RGBA", (w, h))
        self._sprites.append(photo)
        item = self._canvas.create_image(x, y, image=photo, anchor="nw",
                                         tags=("paint",))
        self._fades.append((item, photo, (x, y, w, h), mask))
        return item

    def _paint_card(self, index, record, x, y, w, h, now) -> None:
        canvas = self._canvas
        name = self.store.display_name(record.key, record.label)
        hovered = self._hover_card == index

        frost = self._sprite(render_frost(
            w, h, self.px(16),
            PRISM_CARD_HOVER if hovered else PRISM_CARD,
            PRISM_CARD_EDGE_HOVER if hovered else PRISM_CARD_EDGE,
        ))
        tags = ("paint", "shelf", f"card{index}")
        card_item = canvas.create_image(x, y, image=frost, anchor="nw", tags=tags)
        # Bound on the card's tag rather than its frost, so moving onto the
        # picture or the name keeps the card lit instead of un-hovering it.
        canvas.tag_bind(f"card{index}", "<Enter>",
                        lambda _e, i=index: self._hover(i))
        canvas.tag_bind(f"card{index}", "<Leave>", lambda _e: self._hover(None))

        art_size = self.px(CARD_ART)
        art = load_custom(self.images_dir, record.key, art_size, name) or render_mouse(
            size=art_size, percent=record.percent, online=False
        )
        art_photo = self._sprite(art)
        art_item = canvas.create_image(x + w // 2, y + self.px(10), image=art_photo,
                                       anchor="n", tags=tags)
        self._clickable(art_item,
                        lambda k=record.key, n=name: self._choose_image(k, n),
                        "Choose a picture")

        name_item = canvas.create_text(
            x + w // 2, y + self.px(72), text=name, fill=PRISM_TEXT_SOFT,
            font=("Segoe UI", 8), anchor="n", width=w - self.px(14),
            # Tk left-aligns the lines of wrapped text by default, so a long
            # name's second line sat flush left under a centred first line.
            justify="center", tags=tags,
        )
        self._clickable(name_item, lambda k=record.key, n=name: self._rename(k, n),
                        "Rename")

        level = record.describe_last_known()
        numeric = record.percent is not None
        colour = to_hex(level_color(record.percent))
        age = format_age(now - record.last_online).replace(" ago", "")

        level_item = canvas.create_text(
            x + w // 2, y + self.px(96), text=level, fill=colour,
            font=("Segoe UI", 10 if numeric else 8, "bold"), anchor="n", tags=tags,
        )
        bounds = canvas.bbox(level_item)
        if bounds:
            width_of = bounds[2] - bounds[0]
            gap = self.px(5)
            age_item = canvas.create_text(
                0, 0, text=age, fill=PRISM_TEXT_FAINT,
                font=("Segoe UI", 8), anchor="nw", tags=tags,
            )
            age_bounds = canvas.bbox(age_item)
            age_width = age_bounds[2] - age_bounds[0] if age_bounds else 0
            total = width_of + gap + age_width
            left = x + w // 2 - total // 2
            canvas.coords(level_item, left, y + self.px(96))
            canvas.itemconfigure(level_item, anchor="nw")
            canvas.coords(age_item, left + width_of + gap, y + self.px(98))

        self._card_items[index] = (card_item, w, h)

    def _paint_shelf_header(self, x: int, y: int, w: int, count: int) -> None:
        canvas = self._canvas
        pill_h = self.px(PILL_HEIGHT)
        self._text(x + self.px(4), y + pill_h // 2,
                   f"{count} mouse" if count == 1 else f"{count} mice",
                   fill=PRISM_TEXT_DIM, size=9, anchor="w")

        tags = ("paint", "addmouse")
        label = self._text(0, 0, "+  Add mouse", fill=PRISM_TEXT, size=9,
                           anchor="center", tags=tags)
        left, _, right, _ = canvas.bbox(label)
        pill_w = right - left + self.px(28)
        pill_x = x + w - pill_w
        idle, lit = (
            self._sprite(render_frost(pill_w, pill_h, pill_h // 2, fill, edge))
            for fill, edge in ((PRISM_CARD, PRISM_CARD_EDGE),
                               (PRISM_CARD_HOVER, PRISM_CARD_EDGE_HOVER))
        )
        pill = canvas.create_image(pill_x, y, image=idle, anchor="nw", tags=tags)
        canvas.coords(label, pill_x + pill_w // 2, y + pill_h // 2)
        canvas.tag_raise(label, pill)
        canvas.tag_bind("addmouse", "<Enter>",
                        lambda _e: canvas.itemconfigure(pill, image=lit))
        canvas.tag_bind("addmouse", "<Leave>",
                        lambda _e: canvas.itemconfigure(pill, image=idle))
        self._clickable("addmouse", self._scan,
                        "Find a mouse that is not listed")

    def _hover(self, index: int | None) -> None:
        """Repaint just the cards whose state changed.

        Rebuilding the window would re-render every sprite in it -- eleven
        cards, the panel and the chart -- for a mouse moving across a grid,
        which is exactly the work a hover cannot afford.
        """
        if index == self._hover_card:
            return
        previous, self._hover_card = self._hover_card, index
        for at, hovered in ((previous, False), (index, True)):
            entry = self._card_items.get(at) if at is not None else None
            if entry is None:
                continue
            item, w, h = entry
            photo = self._sprite(render_frost(
                w, h, self.px(16),
                PRISM_CARD_HOVER if hovered else PRISM_CARD,
                PRISM_CARD_EDGE_HOVER if hovered else PRISM_CARD_EDGE,
            ))
            self._canvas.itemconfigure(item, image=photo)

    # ---- settings --------------------------------------------------------

    def _open_settings(self) -> None:
        """The alert threshold and the two switches, in their own window."""
        tk = self._tk
        top = tk.Toplevel(self._root)
        top.title("Settings")
        top.configure(bg=PRISM_GROUND)
        top.resizable(False, False)
        top.transient(self._root)

        body = tk.Frame(top, bg=PRISM_GROUND)
        body.pack(padx=self.px(18), pady=self.px(16))

        row = tk.Frame(body, bg=PRISM_GROUND)
        row.pack(fill="x", pady=(0, self.px(10)))
        tk.Label(row, text="Alert below", bg=PRISM_GROUND, fg=PRISM_TEXT_SOFT,
                 font=("Segoe UI", 9)).pack(side="left")
        threshold = tk.IntVar(value=self.store.alert_threshold)
        tk.Label(row, text="%", bg=PRISM_GROUND, fg=PRISM_TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="right", padx=(self.px(3), 0))
        tk.Spinbox(
            row, from_=MIN_ALERT_THRESHOLD, to=MAX_ALERT_THRESHOLD, increment=5,
            width=3, textvariable=threshold, state="readonly", justify="right",
            font=("Segoe UI", 9), bg=PRISM_HAIRLINE, fg=PRISM_TEXT,
            readonlybackground=PRISM_HAIRLINE, buttonbackground=PRISM_HAIRLINE,
            bd=0, highlightthickness=0,
            command=lambda: self.store.set_alert_threshold(threshold.get()),
        ).pack(side="right")

        notify = tk.BooleanVar(value=self.store.notify_low)
        startup = tk.BooleanVar(value=autostart.is_enabled())

        def check(text, variable, command):
            tk.Checkbutton(
                body, text=text, variable=variable, command=command,
                bg=PRISM_GROUND, fg=PRISM_TEXT_SOFT, activebackground=PRISM_GROUND,
                activeforeground=PRISM_TEXT, selectcolor=PRISM_HAIRLINE,
                font=("Segoe UI", 9), anchor="w", bd=0, highlightthickness=0,
                cursor="hand2",
            ).pack(fill="x", pady=(0, self.px(4)))

        check("Low battery notifications", notify,
              lambda: self.store.set_notify_low(notify.get()))

        def toggle_startup():
            try:
                autostart.toggle()
            except Exception:
                pass
            # Read the registry back: writing the Run key can fail, and a tick
            # left on after a failed write lies about the next login.
            startup.set(autostart.is_enabled())

        check("Start with Windows", startup, toggle_startup)

        top.bind("<Escape>", lambda _e: top.destroy())
        top.protocol("WM_DELETE_WINDOW", lambda: (top.destroy(), self._rebuild()))

    # ---- add mouse -------------------------------------------------------
    #
    # An overlay on the main canvas rather than a second window: a scrim over
    # everything and a frosted card in the panels' style. Each step is a
    # `view` in self._overlay, and _paint_overlay draws the current one from
    # scratch. It also runs at the end of every _rebuild, so a poll landing
    # mid-flow repaints the overlay instead of wiping it.
    #
    # A step's `back` is the state it returns to (Escape, or its Back button);
    # a step without one closes the overlay.

    def _set_overlay(self, **state) -> None:
        self._overlay = state
        self._overlay_seq += 1
        self._overlay_focus = 0
        if state.get("view") == "results":
            self._last_results = state
        self._paint_overlay()

    def _close_overlay(self) -> None:
        self._overlay = None
        self._overlay_seq += 1
        self._paint_overlay()

    def _back(self) -> None:
        back = (self._overlay or {}).get("back")
        if back is None:
            self._close_overlay()
        else:
            self._set_overlay(**back)

    def _on_escape(self, _event=None) -> None:
        if self._overlay is None:
            self._hide()
        else:
            self._back()

    def _on_poll_thread(self, work, next_state) -> None:
        """Run `work` on the poll thread, which owns HID, then show
        `next_state(result)` -- unless the user has moved on since."""
        seq = self._overlay_seq

        def job():
            self._queue.put(("overlay", (seq, next_state(work()))))

        self.run_on_poll_thread(job)

    def _scan(self) -> None:
        from . import probe

        self._set_overlay(
            view="busy", title="Add a mouse", text="Looking for mice",
            hint="Switch the mouse on, or plug in its receiver or cable.",
        )
        self._on_poll_thread(
            probe.scan,
            lambda found: dict(view="results", tracked=found[0], unknown=found[1]),
        )

    def _confirm_trial(self, mouse) -> None:
        self._set_overlay(view="confirm", mouse=mouse, back=self._overlay)

    def _run_trial(self, mouse) -> None:
        back = self._last_results
        self._set_overlay(
            view="busy", title="Trying known protocols", text=f"Asking {mouse.name}",
            hint="Up to half a minute: each supported platform's battery query "
                 "is tried in turn.",
        )

        def work():
            from . import hidio
            from .drivers import adopted

            infos = hidio.enumerate_devices(mouse.vendor_id, mouse.product_id)
            return adopted.try_protocols(infos)

        self._on_poll_thread(
            work, lambda trials: dict(view="trials", mouse=mouse, trials=trials, back=back)
        )

    def _name_placeholder(self, mouse) -> None:
        from .drivers import adopted

        self._ask_name(
            mouse, lambda: adopted.add(adopted.Adoption(mouse.vendor_id, mouse.product_id)),
            "It shows as connected whenever it is plugged in, without a battery level.",
        )

    def _name_adoption(self, mouse, trial) -> None:
        from .drivers import adopted

        adoption = adopted.Adoption(
            mouse.vendor_id, mouse.product_id, trial.profile.driver,
            trial.profile.vendor_id, trial.profile.product_id,
        )
        self._ask_name(
            mouse, lambda: adopted.add(adoption),
            f"Its battery will be read with the {trial.profile.label} protocol.",
        )

    def _ask_name(self, mouse, save, note: str) -> None:
        self._set_overlay(
            view="name", mouse=mouse, save=save, note=note,
            name=self._tk.StringVar(value=mouse.name), back=self._overlay,
        )

    def _finish(self) -> None:
        """Save how the mouse is read, name it, and poll so it shows up."""
        view = self._overlay
        mouse = view["mouse"]
        name = view["name"].get().strip()
        view["save"]()
        if name and name != mouse.name:
            self.store.set_display_name(mouse.key, name)
        self._close_overlay()
        # Shows up on the poll this triggers, which re-reads the saved file.
        if self.on_refresh is not None:
            self.on_refresh("add-mouse")

    def _copy(self, mouse) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(mouse.report)
        self._overlay["copied"] = mouse.key
        self._paint_overlay()

    # ---- overlay: keyboard ----------------------------------------------

    def _overlay_tab(self, step: int):
        if self._overlay is None or not self._overlay_buttons:
            return None
        # The first Tab only reveals where focus is; later ones move it.
        if self._overlay_keyboard:
            self._overlay_focus = (self._overlay_focus + step) % len(self._overlay_buttons)
        self._overlay_keyboard = True
        self._show_overlay_focus()
        return "break"

    def _overlay_activate(self, _event=None):
        if self._overlay is None or not self._overlay_buttons:
            return None
        self._overlay_buttons[self._overlay_focus][0]()
        return "break"

    def _show_overlay_focus(self) -> None:
        for index, (_, ring) in enumerate(self._overlay_buttons):
            shown = self._overlay_keyboard and index == self._overlay_focus
            self._canvas.itemconfigure(ring, state="normal" if shown else "hidden")

    def _tick_dots(self) -> None:
        """Animate the busy line, so a half-minute wait visibly isn't a hang."""
        if self._overlay_dots is None:
            self._dots_running = False
            return
        item, base = self._overlay_dots
        self._dots_count = (self._dots_count + 1) % 4
        try:
            self._canvas.itemconfigure(item, text=base + "." * self._dots_count)
        except Exception:
            pass
        self._root.after(400, self._tick_dots)

    # ---- overlay: painting ----------------------------------------------

    def _paint_overlay(self) -> None:
        from PIL import Image, ImageTk

        canvas = self._canvas
        if canvas is None:
            return
        canvas.delete("overlay")
        if self._overlay_entry is not None:
            self._overlay_entry.destroy()
            self._overlay_entry = None
        self._overlay_sprites = []
        self._overlay_buttons = []
        self._overlay_dots = None
        view = self._overlay
        if view is None:
            self._overlay_keyboard = False
            return

        def sprite(image):
            photo = ImageTk.PhotoImage(image)
            self._overlay_sprites.append(photo)
            return photo

        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < self.px(200) or height < self.px(200):
            width, height = self.px(WINDOW_WIDTH), self.px(WINDOW_HEIGHT)

        # Dims the window, and is the topmost item under the pointer anywhere
        # outside the card, so nothing beneath it reacts to hover or clicks.
        canvas.create_image(0, 0, anchor="nw", tags=("overlay",),
                            image=sprite(Image.new("RGBA", (width, height), PRISM_SCRIM)))

        pad = self.px(24)
        card_w = min(self.px(OVERLAY_WIDTH), width - self.px(32))
        card_x = (width - card_w) // 2
        flow = _Flow(self, card_x + pad, card_w - pad * 2, sprite)
        getattr(self, f"_overlay_{view['view']}")(flow, view)

        # The card's height is whatever its content took, so the content is
        # laid out from y = 0 first and then moved into place.
        card_h = flow.y + pad * 2
        card_y = max(self.px(16), (height - card_h) // 2)
        canvas.move(_Flow.TAG, 0, card_y + pad)
        canvas.create_image(
            card_x, card_y, anchor="nw", tags=("overlay",),
            image=sprite(render_frost(card_w, card_h, self.px(18), PRISM_OVERLAY,
                                      PRISM_PANEL_EDGE)),
        )
        canvas.tag_raise(_Flow.TAG)

        self._overlay_focus = min(self._overlay_focus,
                                  max(0, len(self._overlay_buttons) - 1))
        self._show_overlay_focus()
        if self._overlay_dots is not None and not self._dots_running:
            self._dots_running = True
            self._root.after(400, self._tick_dots)

    def _overlay_busy(self, flow, view) -> None:
        flow.title(view["title"])
        flow.busy(view["text"])
        flow.text(view["hint"], size=8, fill=PRISM_TEXT_DIM, after=0)

    def _overlay_results(self, flow, view) -> None:
        tracked, unknown = view["tracked"], view["unknown"]
        flow.title("Add a mouse")
        if unknown:
            flow.text("Found a device that no driver reads. Try the protocols the "
                      "app already knows, or add it without a battery level.",
                      fill=PRISM_TEXT_DIM, after=14)
        else:
            flow.text("No unrecognised mouse found.", size=10, fill=PRISM_TEXT, after=4)
            flow.text("Switch the mouse on, or plug in its receiver or cable, then "
                      "scan again.", fill=PRISM_TEXT_DIM, after=14)
        for mouse in unknown:
            flow.panel_start()
            flow.text(mouse.name, size=10, fill=PRISM_TEXT, bold=True, after=2)
            flow.text(f"{mouse.vendor_id:04x}:{mouse.product_id:04x}  ·  {mouse.note}",
                      size=8, fill=PRISM_TEXT_DIM, after=12)
            flow.buttons([
                ("Try known protocols", lambda m=mouse: self._confirm_trial(m), "primary"),
                ("Add without battery", lambda m=mouse: self._name_placeholder(m),
                 "secondary"),
                ("Copied" if view.get("copied") == mouse.key else "Copy probe report",
                 lambda m=mouse: self._copy(m), "quiet"),
            ])
            flow.panel_end()
        if tracked:
            names = sorted(self.store.display_name(k, v) for k, v in tracked.items())
            flow.text("Already tracked: " + ", ".join(names), size=8,
                      fill=PRISM_TEXT_FAINT, after=14)
        flow.buttons([("Scan again", self._scan, "secondary" if unknown else "primary")])

    def _overlay_confirm(self, flow, view) -> None:
        from .drivers import adopted

        mouse = view["mouse"]
        flow.title("Try known protocols")
        flow.text(f"This sends {len(adopted.PROFILES)} battery queries, one per "
                  f"supported platform, to {mouse.name}.", after=8)
        flow.text("They are read-only on the mice they were written for, but this "
                  "device has never been tested with them. It takes up to half a "
                  "minute.", size=8, fill=PRISM_TEXT_DIM, after=16)
        flow.buttons([("Send queries", lambda: self._run_trial(mouse), "primary"),
                      ("Back", self._back, "quiet")])

    def _overlay_trials(self, flow, view) -> None:
        mouse, trials = view["mouse"], view["trials"]
        copied = view.get("copied") == mouse.key
        flow.title("Protocol results")
        flow.text(mouse.name, size=9, fill=PRISM_TEXT_SOFT, after=12)
        if not trials:
            flow.text("No known protocol answered.", size=10, fill=PRISM_TEXT, after=4)
            flow.text("Add it without a battery level, or copy the probe report to "
                      "write a driver for it.", fill=PRISM_TEXT_DIM, after=16)
            flow.buttons([
                ("Add without battery", lambda: self._name_placeholder(mouse), "primary"),
                ("Copied" if copied else "Copy probe report",
                 lambda: self._copy(mouse), "secondary"),
                ("Back", self._back, "quiet"),
            ])
            return
        flow.text("Keep one only if it matches what the mouse's own software shows "
                  "right now.", fill=PRISM_TEXT_DIM, after=14)
        for trial in trials:
            percent = trial.reading.percent
            flow.panel_start()
            flow.text(trial.reading.describe(), size=18, family="Segoe UI Light",
                      fill=PRISM_TEXT if percent is None else to_hex(level_color(percent)),
                      after=0)
            flow.text(trial.profile.label, size=8, fill=PRISM_TEXT_DIM, after=12)
            flow.buttons([("It matches, use this",
                           lambda t=trial: self._name_adoption(mouse, t), "primary")])
            flow.panel_end()
        flow.buttons([("Back", self._back, "quiet")])

    def _overlay_name(self, flow, view) -> None:
        flow.title("Name this mouse")
        flow.text(view["note"], fill=PRISM_TEXT_DIM, after=12)
        flow.entry(view["name"])
        flow.text("You can rename it later by clicking its name.", size=8,
                  fill=PRISM_TEXT_FAINT, after=16)
        flow.buttons([("Add mouse", self._finish, "primary"),
                      ("Back", self._back, "quiet")])


class _Flow:
    """Lays overlay content out top to bottom in one column, from y = 0.

    Every item carries TAG, which is what _paint_overlay moves into the card
    once the content's height is known.
    """

    TAG = "overlay_content"
    BUTTON_HEIGHT = 32

    # (fill, border, text) idle and hovered, per button kind. Primary is the
    # panel edge's teal, made solid so dark text on it stays readable.
    STYLES = {
        "primary": (((127, 216, 205, 235), None, PRISM_GROUND),
                    ((164, 234, 225, 245), None, PRISM_GROUND)),
        "secondary": ((PRISM_CARD_HOVER, PRISM_CARD_EDGE_HOVER, PRISM_TEXT),
                      ((52, 55, 72, 220), (255, 255, 255, 80), PRISM_TEXT)),
        "quiet": (((0, 0, 0, 0), None, PRISM_TEXT_DIM),
                  ((255, 255, 255, 20), None, PRISM_TEXT)),
    }

    def __init__(self, hud, x: int, width: int, sprite) -> None:
        self.hud, self.canvas, self.sprite = hud, hud._canvas, sprite
        self.x, self.width, self.y = x, width, 0
        self.indent = 0
        self._panel = None
        self._panels = 0

    @property
    def tags(self):
        tags = ("overlay", self.TAG)
        return tags + (f"ovp{self._panels}",) if self._panel is not None else tags

    def text(self, value, *, size=9, fill=PRISM_TEXT_SOFT, bold=False,
             family="Segoe UI", after=8, width=None):
        font = (family, size, "bold") if bold else (family, size)
        item = self.canvas.create_text(
            self.x + self.indent, self.y, text=value, fill=fill, font=font,
            anchor="nw", width=width or self.width - self.indent * 2, tags=self.tags,
        )
        self.y = self.canvas.bbox(item)[3] + self.hud.px(after)
        return item

    def title(self, value) -> None:
        close = self.hud.px(28)
        self.text(value, size=13, fill=PRISM_TEXT, family="Segoe UI Semibold",
                  after=6, width=self.width - close - self.hud.px(8))
        self._close(self.x + self.width - close, -self.hud.px(4), close)

    def busy(self, value) -> None:
        item = self.text(value + "...", size=10, fill=PRISM_TEXT, after=6)
        self.hud._overlay_dots = (item, value)

    def entry(self, variable) -> None:
        hud = self.hud
        entry = hud._tk.Entry(
            self.canvas, textvariable=variable, font=("Segoe UI", 10),
            bg=PRISM_HAIRLINE, fg=PRISM_TEXT, insertbackground=PRISM_TEXT,
            relief="flat", bd=hud.px(6), highlightthickness=1,
            highlightbackground=PRISM_TEXT_FAINT, highlightcolor=to_hex(PRISM_PANEL_EDGE),
        )
        height = hud.px(36)
        self.canvas.create_window(self.x + self.indent, self.y, window=entry, anchor="nw",
                                  width=self.width - self.indent * 2, height=height,
                                  tags=self.tags)
        hud._overlay_entry = entry
        entry.focus_set()
        entry.icursor("end")
        entry.select_range(0, "end")
        self.y += height + hud.px(8)

    def panel_start(self) -> None:
        """Group what follows on an inset card, until panel_end()."""
        self._panels += 1
        self._panel = self.y
        self.y += self.hud.px(14)
        self.indent = self.hud.px(16)

    def panel_end(self, after=10) -> None:
        px = self.hud.px
        top, tag = self._panel, f"ovp{self._panels}"
        height = self.y - top + px(14)
        item = self.canvas.create_image(
            self.x, top, anchor="nw", tags=self.tags,
            image=self.sprite(render_frost(self.width, height, px(14), PRISM_CARD,
                                           PRISM_CARD_EDGE)),
        )
        self.canvas.tag_lower(item, tag)
        self._panel, self.indent = None, 0
        self.y = top + height + px(after)

    def buttons(self, specs, after=0) -> None:
        """A row of (label, action, kind) buttons, wrapping when it runs out."""
        px = self.hud.px
        height, gap = px(self.BUTTON_HEIGHT), px(8)
        left = self.x + self.indent
        right = self.x + self.width - self.indent
        bx = left
        for label, action, kind in specs:
            width = self._measure(label)
            if bx > left and bx + width > right:
                bx = left
                self.y += height + gap
            self._button(bx, self.y, width, height, label, action, kind)
            bx += width + gap
        self.y += height + px(after)

    def _measure(self, label) -> int:
        item = self.canvas.create_text(0, 0, text=label, font=("Segoe UI", 9))
        left, _, right, _ = self.canvas.bbox(item)
        self.canvas.delete(item)
        return right - left + self.hud.px(28)

    def _button(self, x, y, width, height, label, action, kind) -> None:
        canvas, px = self.canvas, self.hud.px
        (fill, border, ink), (lit_fill, lit_border, lit_ink) = self.STYLES[kind]
        idle = self.sprite(render_frost(width, height, height // 2, fill, border))
        lit = self.sprite(render_frost(width, height, height // 2, lit_fill, lit_border))
        tag = f"ovb{len(self.hud._overlay_buttons)}"
        tags = self.tags + (tag,)
        ring_pad = px(3)
        ring = canvas.create_image(
            x - ring_pad, y - ring_pad, anchor="nw", state="hidden", tags=tags,
            image=self.sprite(render_frost(
                width + ring_pad * 2, height + ring_pad * 2, (height + ring_pad * 2) // 2,
                (0, 0, 0, 0), border=(255, 255, 255, 220), border_width=2,
            )),
        )
        bg = canvas.create_image(x, y, anchor="nw", image=idle, tags=tags)
        text = canvas.create_text(x + width // 2, y + height // 2, text=label, fill=ink,
                                  font=("Segoe UI", 9), tags=tags)

        def enter(_event):
            canvas.configure(cursor="hand2")
            canvas.itemconfigure(bg, image=lit)
            canvas.itemconfigure(text, fill=lit_ink)

        def leave(_event):
            canvas.configure(cursor="")
            canvas.itemconfigure(bg, image=idle)
            canvas.itemconfigure(text, fill=ink)

        def click(_event):
            canvas.configure(cursor="")
            action()

        canvas.tag_bind(tag, "<Enter>", enter)
        canvas.tag_bind(tag, "<Leave>", leave)
        canvas.tag_bind(tag, "<Button-1>", click)
        self.hud._overlay_buttons.append((action, ring))

    def _close(self, x, y, size) -> None:
        """The card's close button: an X that lights up on hover."""
        from PIL import Image

        canvas, px = self.canvas, self.hud.px
        tag = "ovclose"
        tags = self.tags + (tag,)
        # A clear image still hit-tests by its box, which is the click target.
        idle = self.sprite(Image.new("RGBA", (size, size), (0, 0, 0, 0)))
        lit = self.sprite(render_frost(size, size, size // 2, PRISM_CARD_HOVER))
        bg = canvas.create_image(x, y, anchor="nw", image=idle, tags=tags)
        arm = size * 0.18
        cx, cy = x + size / 2, y + size / 2
        lines = [
            canvas.create_line(cx - arm, cy - arm, cx + arm, cy + arm, fill=PRISM_TEXT_DIM,
                               width=max(1, px(1.5)), capstyle="round", tags=tags),
            canvas.create_line(cx - arm, cy + arm, cx + arm, cy - arm, fill=PRISM_TEXT_DIM,
                               width=max(1, px(1.5)), capstyle="round", tags=tags),
        ]

        def paint(on):
            canvas.configure(cursor="hand2" if on else "")
            canvas.itemconfigure(bg, image=lit if on else idle)
            for line in lines:
                canvas.itemconfigure(line, fill=PRISM_TEXT if on else PRISM_TEXT_DIM)

        canvas.tag_bind(tag, "<Enter>", lambda _e: paint(True))
        canvas.tag_bind(tag, "<Leave>", lambda _e: paint(False))
        canvas.tag_bind(tag, "<Button-1>", lambda _e: self.hud._close_overlay())
