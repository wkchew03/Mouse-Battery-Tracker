"""Draws the mouse graphic shown in the HUD.

Deliberately drawn rather than shipped as photos: vendor product images are
copyrighted, and any bundled set would simply have no entry for a mouse the app
has never seen. A drawn silhouette works for every device, including the ones
whose own firmware calls them "PIAO-2.4G".

The silhouette doubles as the gauge -- it fills from the bottom in proportion to
charge -- so a glance at the shape reads as a battery level without the number.

Users can override any of this by dropping a PNG into the images/ folder named
after the device key; see `custom_image_path`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .theme import COLOR_UNKNOWN, HUD_TRACK, level_color

OUTLINE = (232, 233, 237, 255)
OUTLINE_DIM = (122, 124, 132, 255)
EMPTY = (58, 61, 70, 255)


_ILLEGAL = '<>:"/\\|?*'

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def safe_filename(text: str) -> str:
    """Filename-safe form of a name, preserving spaces and case."""
    cleaned = "".join("_" if ch in _ILLEGAL else ch for ch in text).strip()
    return cleaned.rstrip(". ") or "mouse"


def custom_image_path(directory: Path, key: str, name: str = "") -> Path:
    """Preferred location for a user-supplied image.

    Named after the mouse when there is a name, so one file covers a mouse that
    enumerates differently wired vs wireless. The key is only a fallback.
    """
    return directory / f"{safe_filename(name or key.replace(':', '_'))}.png"


def candidate_paths(directory: Path, key: str, name: str = "") -> list[Path]:
    """Every filename accepted for this mouse, most specific first."""
    stems: list[str] = []
    if name:
        stems.append(safe_filename(name))
    stems.append(safe_filename(key.replace(":", "_")))
    stems.append(key.replace(":", "_"))

    paths: list[Path] = []
    for stem in dict.fromkeys(stems):
        for suffix in IMAGE_SUFFIXES:
            paths.append(directory / f"{stem}{suffix}")
    return paths


def load_custom(
    directory: Path, key: str, size: int, name: str = ""
) -> Image.Image | None:
    image = None
    for path in candidate_paths(directory, key, name):
        try:
            image = Image.open(path).convert("RGBA")
            break
        except (OSError, ValueError):
            continue
    if image is None:
        return None
    image.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        image, ((size - image.width) // 2, (size - image.height) // 2)
    )
    return canvas


# Pillow's draw primitives are not antialiased, so every rounded edge comes out
# hard-jagged. Drawing at this multiple and downscaling with LANCZOS is the
# standard fix and costs nothing at these sizes.
SUPERSAMPLE = 4


def render_mouse(
    size: int = 96,
    percent: int | None = None,
    charging: bool = False,
    online: bool = True,
) -> Image.Image:
    """Top-down mouse silhouette filled from the bottom to `percent`."""
    target = size
    size = size * SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Body proportions: a real mouse is taller than it is wide, ~0.62 ratio.
    width = size * 0.56
    height = size * 0.86
    x0 = (size - width) / 2
    y0 = (size - height) / 2
    x1 = x0 + width
    y1 = y0 + height
    radius = width * 0.46

    # 1. Silhouette mask.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=255)

    # 2. Fill layer: empty track, then the charged portion from the bottom up.
    #
    # A known level is drawn whether or not the mouse is currently reachable --
    # an offline mouse still has a last-known charge, and blanking it made every
    # disconnected mouse render as an identical empty shape. Offline levels use
    # the muted colour so they read as stale rather than live.
    fill = Image.new("RGBA", (size, size), EMPTY)
    if percent is not None:
        level = level_color(percent, charging) if online else COLOR_UNKNOWN
        top = y1 - (y1 - y0) * (max(0, min(100, percent)) / 100)
        ImageDraw.Draw(fill).rectangle((0, top, size, size), fill=level)

    fill.putalpha(mask)
    image.alpha_composite(fill)

    # 3. Outline and details on top.
    draw = ImageDraw.Draw(image)
    outline = OUTLINE if online else OUTLINE_DIM
    draw.rounded_rectangle(
        (x0, y0, x1, y1), radius=radius, outline=outline, width=max(2, size // 40)
    )

    # Button split: horizontal divide at ~45%, vertical seam above it.
    split_y = y0 + height * 0.44
    line_width = max(1, size // 64)
    draw.line((x0 + line_width, split_y, x1 - line_width, split_y),
              fill=outline, width=line_width)
    draw.line((size / 2, y0 + radius * 0.35, size / 2, split_y),
              fill=outline, width=line_width)

    # Scroll wheel.
    wheel_w = width * 0.13
    wheel_h = height * 0.13
    draw.rounded_rectangle(
        (size / 2 - wheel_w / 2, y0 + height * 0.16,
         size / 2 + wheel_w / 2, y0 + height * 0.16 + wheel_h),
        radius=wheel_w / 2,
        fill=outline,
    )
    return image.resize((target, target), Image.LANCZOS)


def install_image(directory: Path, key: str, name: str, source: Path) -> Path | None:
    """Copy a chosen image into place under the name this mouse looks for.

    Saved as PNG so one predictable filename works regardless of what the user
    picked, and any existing images for the same mouse are cleared first so a
    stale higher-priority file can't win.
    """
    try:
        image = Image.open(source).convert("RGBA")
    except (OSError, ValueError):
        return None

    directory.mkdir(parents=True, exist_ok=True)
    for path in candidate_paths(directory, key, name):
        try:
            path.unlink()
        except OSError:
            pass

    target = custom_image_path(directory, key, name)
    try:
        image.save(target, "PNG")
    except (OSError, ValueError):
        return None
    return target


__all__ = [
    "candidate_paths",
    "custom_image_path",
    "install_image",
    "load_custom",
    "render_mouse",
    "safe_filename",
]
