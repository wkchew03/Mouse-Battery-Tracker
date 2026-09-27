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
    PRISM_PANEL_EDGE,
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
PANEL_ART = 120
PANEL_ART_HEIGHT = 166
CHART_HEIGHT = 92

CARD_HEIGHT = 126
CARD_ART = 56
CARD_GAP = 10
GRID_COLUMNS = 3

FADE_HEIGHT = 64
TOP_FADE_HEIGHT = 40

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
        root.bind("<Escape>", lambda _event: self._hide())

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
        if canvas is None or self._scroll_limit <= 0:
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

        art_size = self.px(PANEL_ART)
        art = load_custom(self.images_dir, key, art_size, name) or render_mouse(
            size=art_size, percent=percent, charging=bool(reading.charging), online=True
        )
        art_photo = self._sprite(art)
        art_item = canvas.create_image(
            x + w // 2, y + self.px(30), image=art_photo, anchor="n", tags=("paint",)
        )
        self._clickable(art_item, lambda k=key, n=name: self._choose_image(k, n),
                        "Choose a picture")

        top = y + self.px(30) + self.px(PANEL_ART_HEIGHT)

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
        online_keys = {key for key, _, _ in self._online}
        records = [r for r in self.store.recent(exclude=online_keys) if r.last_online]

        gap = self.px(CARD_GAP)
        card_w = (w - gap * (GRID_COLUMNS - 1)) // GRID_COLUMNS
        card_h = self.px(CARD_HEIGHT)
        now = time.time()

        self._card_items = {}
        for index, record in enumerate(records):
            column = index % GRID_COLUMNS
            row = index // GRID_COLUMNS
            cx = x + column * (card_w + gap)
            cy = y + row * (card_h + gap) - self._scroll
            self._paint_card(index, record, cx, cy, card_w, card_h, now)

        rows = (len(records) + GRID_COLUMNS - 1) // GRID_COLUMNS
        content = rows * card_h + max(0, rows - 1) * gap
        self._scroll_limit = max(0.0, content - h)

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
            self._fade_top = self._add_fade(
                x, 0, w, y + top_ramp,
                fade_mask(w, y + top_ramp, top_ramp, reverse=True))
        self._update_fades()

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
            font=("Segoe UI", 8), anchor="n", width=w - self.px(14), tags=tags,
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
