"""Shared colours for the tray icon and the HUD window.

Kept free of any UI-toolkit import so both pystray and tkinter code can use it,
and so the colour rules stay unit-testable on their own.
"""

from __future__ import annotations

RGBA = tuple[int, int, int, int]

COLOR_HIGH: RGBA = (76, 187, 106, 255)
COLOR_MID: RGBA = (232, 168, 56, 255)
COLOR_LOW: RGBA = (222, 74, 62, 255)
COLOR_UNKNOWN: RGBA = (140, 140, 148, 255)
COLOR_CHARGING: RGBA = (74, 158, 232, 255)

# HUD surfaces. Dark by default: this window is opened mid-session, often over a
# game, and a white panel is jarring in that context.
HUD_BG = "#1e1f24"
HUD_CARD = "#2a2c33"
HUD_CARD_DIM = "#24262c"
HUD_TEXT = "#e8e9ed"
# Names of disconnected mice: clearly dimmer than a live name, but still
# brighter than HUD_MUTED so the name/timestamp hierarchy survives.
HUD_TEXT_DIM = "#b0b2bc"
HUD_MUTED = "#9a9ca6"
HUD_TRACK = "#3a3d46"
# Slightly lifted surface for the hero card, so the connected mouse reads as
# the primary element without needing a different hue.
HUD_HERO = "#31343d"

THRESHOLD_HIGH = 50
THRESHOLD_LOW = 20


def level_color(percent: int | None, charging: bool = False) -> RGBA:
    """Colour for a battery level. Charging outranks level."""
    if charging:
        return COLOR_CHARGING
    if percent is None:
        return COLOR_UNKNOWN
    if percent >= THRESHOLD_HIGH:
        return COLOR_HIGH
    if percent >= THRESHOLD_LOW:
        return COLOR_MID
    return COLOR_LOW


# How far to darken a colour for a reading that is no longer live. Chosen to
# stay clearly readable against HUD_CARD while reading as obviously secondary
# next to the connected mouse.
DIM_FACTOR = 0.6


def dim(color: RGBA, factor: float = DIM_FACTOR) -> RGBA:
    """Darkened variant of a level colour, for disconnected mice.

    Keeps the hue so the level still reads at a glance -- a stale 90% is still
    green, just muted -- rather than flattening everything to grey.
    """
    return (
        int(color[0] * factor),
        int(color[1] * factor),
        int(color[2] * factor),
        color[3] if len(color) > 3 else 255,
    )


def to_hex(color: RGBA) -> str:
    """RGBA tuple -> '#rrggbb', for tkinter which has no alpha."""
    return "#{:02x}{:02x}{:02x}".format(*color[:3])
