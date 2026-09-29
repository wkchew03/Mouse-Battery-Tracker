"""The drifting colour field behind the HUD.

Rendered once, oversized, and then *panned* rather than re-rendered. That is
the whole design of this module, and it is what makes an animated background
affordable in an app that advertises ~45 MB and no GPU context:

- A pre-rendered animation loop is the obvious approach and the wrong one. At
  900x600 a single Tk photo image costs ~2 MB, so even a short 60-frame loop
  is ~130 MB resident -- three times the whole app's budget.
- Re-rendering per frame costs ~6 ms, which is affordable on its own but has
  to be paid again as a photo image conversion on every tick.
- Panning one image costs a `coords()` call: no allocation, no conversion, no
  per-frame Python work at all.

The field is entirely low-frequency -- three heavily blurred blobs -- so it is
built at a quarter of its final size and scaled up, and a rigid translation
reads as the same slow drift the design asked for.

Nothing here touches Tk; it returns PIL images so it stays testable.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter

from .theme import PRISM_FIELDS, PRISM_GROUND_RGB

# How much larger than the window the field is rendered, so panning never
# exposes an edge. 1.45 gives roughly +/-20% of travel in each direction.
OVERSCAN = 1.45

# The field is blur-only, so it is built at this fraction of its final size.
# Anything sharper is thrown away by the blur that follows.
DRAFT_SCALE = 0.25

# Seconds for one full circuit of the drift path.
PERIOD_SECONDS = 48.0

# Diagonal grain, straight from the design. Faint enough to be texture rather
# than pattern -- it stops the large flat gradients banding on 8-bit displays.
GRAIN_ALPHA = 5
GRAIN_SPACING = 4


def render_field(width: int, height: int) -> Image.Image:
    """The oversized, blurred colour field. Expensive; call once per size."""
    full = (max(1, int(width * OVERSCAN)), max(1, int(height * OVERSCAN)))
    draft = (max(1, int(full[0] * DRAFT_SCALE)), max(1, int(full[1] * DRAFT_SCALE)))

    field = Image.new("RGBA", draft, (*PRISM_GROUND_RGB, 255))
    for colour, (cx, cy), radius_frac, opacity in PRISM_FIELDS:
        # Composited, not blended. Image.blend averages the whole canvas, so
        # each new blob halves the ones already there and the field ends up a
        # flat grey wash -- which is exactly what the first build looked like.
        layer = Image.new("RGBA", draft, (0, 0, 0, 0))
        radius = radius_frac * max(draft)
        x, y = cx * draft[0], cy * draft[1]
        ImageDraw.Draw(layer).ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(*colour, int(255 * opacity)),
        )
        field = Image.alpha_composite(field, layer)
    field = field.convert("RGB")

    # One blur at draft size does the work of a much larger one at full size.
    # 0.055 of the draft is ~72px at full size, matching the design's blur.
    field = field.filter(ImageFilter.GaussianBlur(radius=max(draft) * 0.055))
    # Bilinear, not LANCZOS: the draft is all blur, so there is no detail for
    # a sharper filter to keep, and at 4K LANCZOS was most of a resize.
    field = field.resize(full, Image.BILINEAR)
    return _add_grain(field)


def _add_grain(image: Image.Image) -> Image.Image:
    """Faint diagonal texture, drawn once into the field."""
    width, height = image.size
    # An "RGBA" draw on an RGB image blends each line in place: the same
    # pixels as compositing a grain layer, without three full-size copies.
    draw = ImageDraw.Draw(image, "RGBA")
    # 115 degrees in the design; drawn as lines with a slope of that angle.
    slope = math.tan(math.radians(115))
    reach = width + abs(int(height / slope)) if slope else width
    for offset in range(-reach, reach, GRAIN_SPACING):
        draw.line(
            (offset, 0, offset + int(height / slope), height),
            fill=(255, 255, 255, GRAIN_ALPHA),
            width=1,
        )
    return image


def offset_at(elapsed: float, width: int, height: int) -> tuple[int, int]:
    """Top-left corner for the field at `elapsed` seconds.

    A Lissajous path with a 2:3 ratio, so the drift never retraces the same
    line and the loop is seamless without a stored frame sequence.
    """
    field_w = int(width * OVERSCAN)
    field_h = int(height * OVERSCAN)
    slack_x = field_w - width
    slack_y = field_h - height

    phase = (elapsed % PERIOD_SECONDS) / PERIOD_SECONDS * 2 * math.pi
    # sin/cos of different harmonics keeps x and y from moving in lockstep.
    fx = (math.sin(phase * 2) + 1) / 2
    fy = (math.cos(phase * 3) + 1) / 2
    return -int(slack_x * fx), -int(slack_y * fy)


class Field:
    """A rendered field plus the pan schedule for it."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image = render_field(width, height)

    def matches(self, width: int, height: int) -> bool:
        return self.width == width and self.height == height

    def offset(self, elapsed: float) -> tuple[int, int]:
        return offset_at(elapsed, self.width, self.height)


__all__ = ["Field", "OVERSCAN", "PERIOD_SECONDS", "offset_at", "render_field"]
