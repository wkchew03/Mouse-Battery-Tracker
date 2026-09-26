"""Frosted panels, the discharge chart, fades and icons for the HUD.

Tkinter has no rounded corners, alpha or dashed lines, so these are drawn with
Pillow and handed over as RGBA sprites.

Everything is supersampled and downscaled: Pillow does not antialias, and a
hard-edged circle looks obviously wrong next to Windows' own UI.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .theme import PRISM_GROUND

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


def render_frost(
    width: int,
    height: int,
    radius: int,
    fill,
    border=None,
    border_width: int = 1,
) -> Image.Image:
    """A translucent rounded card, as an RGBA sprite.

    Returned with its alpha intact rather than pre-composited: a Tk photo
    image blends against whatever canvas item is beneath it, so the drifting
    field shows through exactly as the design's backdrop blur does. The blur
    itself is already in the field -- it is nothing but low-frequency colour,
    so there is no high-frequency detail for a second blur to remove.
    """
    scale = 2
    card = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale,
        fill=_rgba(fill),
        outline=_rgba(border) if border else None,
        width=border_width * scale if border else 0,
    )
    return card.resize((width, height), Image.LANCZOS)


def _dashed_line(draw, x0: float, y: float, x1: float, colour, dash: int = 3) -> None:
    """Pillow has no dash pattern, so the segments are drawn individually."""
    x = x0
    while x < x1:
        draw.line((x, y, min(x + dash, x1), y), fill=colour, width=1)
        x += dash * 2


def render_discharge_chart(
    width: int,
    height: int,
    points: list[tuple[float, int]],
    colour,
    threshold: int | None = None,
    hover: int | None = None,
) -> Image.Image:
    """Battery level over the current discharge window.

    `points` are (seconds from the start of the window, percent), oldest
    first. The y axis is always the full 0-100: these mice drop a handful of
    points over hours, and scaling to fit would redraw a gentle slope as a
    cliff. The caption underneath is a canvas text item, not drawn in here --
    baked text was clipped by the image's own edge.
    """
    scale = SUPERSAMPLE
    w, h = width * scale, height * scale
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    top = 6 * scale
    base = h - 6 * scale
    ink = _rgba(colour)

    def y_for(percent: float) -> float:
        return top + (1 - max(0.0, min(100.0, percent)) / 100.0) * (base - top)

    draw.line((0, base, w, base), fill=(255, 255, 255, 36), width=1 * scale)

    plotted = []
    if len(points) >= 2:
        span = points[-1][0] - points[0][0]
        span = span if span > 0 else 1.0
        for seconds, percent in points:
            x = ((seconds - points[0][0]) / span) * (w - 2 * scale) + scale
            plotted.append((x, y_for(percent)))

        area = [(plotted[0][0], base)] + plotted + [(plotted[-1][0], base)]
        draw.polygon(area, fill=(*ink[:3], 20))
        draw.line(plotted, fill=ink, width=2 * scale, joint="curve")

    # Drawn after the fill so it stays legible through it, and before the
    # markers so a low battery's line still sits on top of its own alert rule.
    if threshold is not None:
        ty = y_for(threshold)
        _dashed_line(draw, 0, ty, w, (255, 255, 255, 76), dash=3 * scale)
        font = load_font(int(8.5 * scale))
        draw.text((3 * scale, ty - 13 * scale), f"alert {threshold}%", font=font,
                  fill=(232, 233, 237, 140))

    if plotted:
        end = plotted[-1]
        radius = 4 * scale
        draw.ellipse((end[0] - radius, end[1] - radius, end[0] + radius, end[1] + radius),
                     fill=ink, outline=_rgba(PRISM_GROUND), width=2 * scale)

        if hover is not None and 0 <= hover < len(plotted):
            hx, hy = plotted[hover]
            draw.line((hx, top - 4 * scale, hx, base), fill=(255, 255, 255, 76), width=1 * scale)
            r = 4.5 * scale
            draw.ellipse((hx - r, hy - r, hx + r, hy + r), fill=ink,
                         outline=_rgba(PRISM_GROUND), width=2 * scale)

    return image.resize((width, height), Image.LANCZOS)


def render_fade(width: int, height: int, colour, reverse: bool = False) -> Image.Image:
    """Vertical fade to the ground colour, standing in for a scrollbar.

    The list scrolls by wheel with no bar; this is what says there is more
    below (or above) it.
    """
    base = _rgba(colour)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        if reverse:
            t = 1 - t
        draw.line((0, y, width, y), fill=(*base[:3], int(240 * (t ** 1.6))))
    return image



def render_icon(name: str, size: int, colour) -> Image.Image:
    """A small stroked icon. Drawn rather than set as a glyph, because a font
    that lacks the character silently renders an empty box instead."""
    scale = SUPERSAMPLE
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ink = _rgba(colour)
    stroke = max(1, int(s * 0.075))

    if name == "gear":
        outer = s * 0.30
        inner = s * 0.12
        centre = s / 2
        for step in range(8):
            angle = step * math.pi / 4
            x = centre + math.cos(angle) * outer * 1.32
            y = centre + math.sin(angle) * outer * 1.32
            tooth = s * 0.07
            draw.ellipse((x - tooth, y - tooth, x + tooth, y + tooth), fill=ink)
        draw.ellipse((centre - outer, centre - outer, centre + outer, centre + outer),
                     outline=ink, width=stroke)
        draw.ellipse((centre - inner, centre - inner, centre + inner, centre + inner),
                     outline=ink, width=stroke)
    elif name == "folder":
        left, right = s * 0.12, s * 0.88
        top, bottom = s * 0.26, s * 0.78
        draw.line((left, top + s * 0.06, left + s * 0.28, top + s * 0.06),
                  fill=ink, width=stroke)
        draw.line((left + s * 0.28, top + s * 0.06, left + s * 0.36, top),
                  fill=ink, width=stroke)
        draw.rounded_rectangle((left, top, right, bottom), radius=int(s * 0.08),
                               outline=ink, width=stroke)

    return image.resize((size, size), Image.LANCZOS)
