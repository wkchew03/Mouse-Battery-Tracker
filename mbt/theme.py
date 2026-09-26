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
HUD_TEXT = "#e8e9ed"
HUD_MUTED = "#9a9ca6"
HUD_TRACK = "#3a3d46"

THRESHOLD_HIGH = 50
THRESHOLD_LOW = 20

# ---- Prism surfaces -------------------------------------------------------
# The window is a near-black ground with three slow, heavily blurred colour
# fields drifting behind frosted panels. Hues were chosen to sit clear of the
# green/amber/red band the battery levels own, so a level colour never has to
# compete with the background it is read against.
PRISM_GROUND = "#0d0d12"
PRISM_GROUND_RGB: tuple[int, int, int] = (13, 13, 18)

# (colour, centre as a fraction of the field, radius fraction, opacity)
PRISM_FIELDS = (
    ((23, 184, 166), (0.20, 0.16), 0.42, 0.50),   # teal
    ((91, 91, 214), (0.82, 0.22), 0.40, 0.52),    # indigo
    ((214, 74, 143), (0.22, 0.86), 0.33, 0.34),   # magenta, kept low and corner-bound
)

# Frosted surfaces, RGBA so they composite over the drifting field. Tk photo
# images blend against whatever canvas item sits beneath them.
PRISM_CARD: RGBA = (18, 19, 26, 143)
PRISM_CARD_HOVER: RGBA = (34, 36, 48, 189)
PRISM_CARD_EDGE: RGBA = (255, 255, 255, 20)
PRISM_CARD_EDGE_HOVER: RGBA = (255, 255, 255, 46)
PRISM_PANEL: RGBA = (16, 17, 24, 128)
PRISM_PANEL_EDGE: RGBA = (127, 216, 205, 128)

# Canvas text has no alpha, so these are the design's translucent inks already
# blended against the frosted card they sit on.
PRISM_TEXT = "#ffffff"
PRISM_TEXT_SOFT = "#d5d7dc"
PRISM_TEXT_DIM = "#9a9ba1"
PRISM_TEXT_FAINT = "#777984"
PRISM_HAIRLINE = "#2b2d38"


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


def to_hex(color: RGBA) -> str:
    """RGBA tuple -> '#rrggbb', for tkinter which has no alpha."""
    return "#{:02x}{:02x}{:02x}".format(*color[:3])
