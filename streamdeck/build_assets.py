"""Generate the static images manifest.json requires.

Reuses the app's own mouse artwork so the plugin looks like the tray icon and
the HUD rather than being a separate visual language.

    python streamdeck/build_assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw  # noqa: E402

from mbt.mouseart import render_mouse  # noqa: E402
from mbt.theme import HUD_BG, HUD_MUTED, HUD_TEXT  # noqa: E402

PLUGIN = Path(__file__).resolve().parent / "com.kai.mousebattery.sdPlugin"
IMGS = PLUGIN / "imgs"

BG = (30, 31, 36, 255)


def _hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    return (*(int(value[i : i + 2], 16) for i in (0, 2, 4)), 255)


def mouse_tile(size: int, percent: int | None, transparent: bool = False) -> Image.Image:
    """Mouse artwork centred on a tile, sized for a Stream Deck asset."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else BG)
    art = render_mouse(size=int(size * 0.82), percent=percent, online=True)
    offset = ((size - art.width) // 2, (size - art.height) // 2)
    canvas.alpha_composite(art, offset)
    return canvas


def save(image: Image.Image, relative: str) -> None:
    target = IMGS / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target.with_suffix(".png"), "PNG")
    print(f"  {target.relative_to(PLUGIN)}.png  {image.width}x{image.height}")


def background(width: int, height: int) -> Image.Image:
    """Touchscreen backdrop for the dial layout."""
    image = Image.new("RGBA", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline=_hex_to_rgba(HUD_MUTED))
    return image


def main() -> int:
    print("writing plugin assets:")

    # Plugin icon (preferences) 256 / 512
    save(mouse_tile(256, 80), "plugin/icon")
    save(mouse_tile(512, 80), "plugin/icon@2x")

    # Category icon 28 / 56 -- transparent so it sits on the list background
    save(mouse_tile(28, 80, transparent=True), "plugin/category")
    save(mouse_tile(56, 80, transparent=True), "plugin/category@2x")

    # Action icon in the actions list 20 / 40
    save(mouse_tile(20, 80, transparent=True), "actions/battery/icon")
    save(mouse_tile(40, 80, transparent=True), "actions/battery/icon@2x")

    # Default key image 72 / 144 (replaced at runtime by the live gauge)
    save(mouse_tile(72, None), "actions/battery/key")
    save(mouse_tile(144, None), "actions/battery/key@2x")

    # Encoder circular icon 72 / 144
    save(mouse_tile(72, 80, transparent=True), "actions/battery/encoder")
    save(mouse_tile(144, 80, transparent=True), "actions/battery/encoder@2x")

    # Encoder touchscreen background 200x100 / 400x200
    save(background(200, 100), "actions/battery/background")
    save(background(400, 200), "actions/battery/background@2x")

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
