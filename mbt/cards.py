"""Card backgrounds and the circular battery ring.

Tkinter has no rounded corners, shadows or arc widgets, so these are drawn with
Pillow and handed over as images. They are cached by their arguments because the
HUD rebuilds its whole tree on every refresh, and re-rendering a ring on each
poll would be wasteful for a picture that rarely changes.

Everything is supersampled and downscaled: Pillow does not antialias, and a
hard-edged circle looks obviously wrong next to Windows' own UI.
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .theme import (
    COLOR_UNKNOWN,
    HUD_CARD,
    HUD_TRACK,
    level_color,
    to_hex,
)

SUPERSAMPLE = 4

_FONT_CANDIDATES = (
    "segoeuib.ttf",
    "seguisb.ttf",
    "arialbd.ttf",
    "DejaVuSans-Bold.ttf",
)


@lru_cache(maxsize=64)
def load_font(size: int) -> ImageFont.ImageFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rgba(value) -> tuple[int, int, int, int]:
    """Accept either an RGBA tuple or a '#rrggbb' string."""
    if isinstance(value, str):
        value = value.lstrip("#")
        return (*(int(value[i : i + 2], 16) for i in (0, 2, 4)), 255)
    if len(value) == 3:
        return (*value, 255)
    return tuple(value)


@lru_cache(maxsize=128)
def render_card(
    width: int,
    height: int,
    radius: int = 14,
    fill: str = HUD_CARD,
    border: str | None = None,
) -> Image.Image:
    """Rounded card background."""
    scale = 2
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale,
        fill=_rgba(fill),
        outline=_rgba(border) if border else None,
        width=scale if border else 0,
    )
    return image.resize((width, height), Image.LANCZOS)


@lru_cache(maxsize=128)
def render_ring(
    size: int,
    percent: int | None,
    charging: bool = False,
    online: bool = True,
    thickness: int | None = None,
) -> Image.Image:
    """Circular gauge with the percentage in the middle.

    Sweeps clockwise from twelve o'clock, which is what makes it read as a
    progress dial rather than a pie chart.
    """
    thickness = thickness or max(6, size // 9)
    big = size * SUPERSAMPLE
    stroke = thickness * SUPERSAMPLE

    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    inset = stroke // 2 + SUPERSAMPLE
    box = (inset, inset, big - inset, big - inset)

    draw.arc(box, 0, 360, fill=_rgba(HUD_TRACK), width=stroke)

    if percent is not None:
        colour = level_color(percent, charging) if online else COLOR_UNKNOWN
        sweep = 360 * max(0, min(100, percent)) / 100
        if sweep > 0:
            # -90 puts the start at the top.
            draw.arc(box, -90, -90 + sweep, fill=_rgba(colour), width=stroke)

    text = "--" if percent is None else f"{percent}%"
    font = load_font(int(big * (0.24 if len(text) <= 3 else 0.20)))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    position = (
        (big - (right - left)) / 2 - left,
        (big - (bottom - top)) / 2 - top,
    )
    draw.text(position, text, font=font, fill=_rgba("#e8e9ed"))

    return image.resize((size, size), Image.LANCZOS)


def status_dot(size: int, colour) -> Image.Image:
    """Small filled circle for the 'Active' indicator."""
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((0, 0, big - 1, big - 1), fill=_rgba(colour))
    return image.resize((size, size), Image.LANCZOS)


__all__ = ["load_font", "render_card", "render_ring", "status_dot", "to_hex"]
