"""Per-monitor DPI awareness.

Without this, Windows renders the window at 96 DPI and bitmap-stretches the
result to the real display scale. On a 4K screen at 150% that means everything
is drawn at 2560x1440 and blown up to 3840x2160 -- text and edges come out
visibly soft.

Declaring awareness makes Tk draw at true device pixels. The catch is that
everything then measures in real pixels, so sizes must be multiplied by
`scale()` or the UI comes out physically tiny.

Must be called before the process creates any window.
"""

from __future__ import annotations

import ctypes

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_CONTEXT_PER_MONITOR_V2 = ctypes.c_void_p(-4)
_PROCESS_PER_MONITOR_DPI_AWARE = 2
_LOGPIXELSX = 88


def enable() -> bool:
    """Declare the process DPI-aware. Returns True if any method succeeded.

    Tries newest to oldest: the per-monitor-v2 context (Win10 1703+), the
    Win8.1 shcore call, then the legacy system-wide flag.
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(_CONTEXT_PER_MONITOR_V2):
            return True
    except (AttributeError, OSError):
        pass
    try:
        # Returns an HRESULT; 0 is S_OK. E_ACCESSDENIED means it was already set,
        # which is equally fine for our purposes.
        result = ctypes.windll.shcore.SetProcessDpiAwareness(
            _PROCESS_PER_MONITOR_DPI_AWARE
        )
        if result in (0, -2147024891):
            return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


def system_dpi() -> int:
    """Effective DPI of the primary display; 96 when unavailable."""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return 96
    try:
        return int(user32.GetDpiForSystem()) or 96
    except (AttributeError, OSError):
        pass
    try:
        hdc = user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, _LOGPIXELSX)
        user32.ReleaseDC(0, hdc)
        return int(dpi) or 96
    except (AttributeError, OSError):
        return 96


def scale() -> float:
    """Display scale factor: 1.0 at 96 DPI, 1.5 at 144 DPI."""
    return system_dpi() / 96.0


def tk_scaling() -> float:
    """Value for Tk's `tk scaling` -- pixels per typographic point."""
    return system_dpi() / 72.0
