"""Opt-in "start with Windows" via the per-user Run key.

HKCU only -- never HKLM. This touches nothing outside the current user's own
autostart list, needs no elevation, and is trivially reversible from the same
menu item that set it.

Nothing here runs unless the user ticks the menu item. The app never enables
its own autostart.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MouseBatteryTracker"


def _winreg():
    try:
        import winreg
    except ImportError:  # non-Windows
        return None
    return winreg


def _command_for_registry() -> str:
    """Command Windows runs at login: pythonw.exe (no console) launching the tray.

    Falls back to sys.executable when pythonw is absent, e.g. a frozen build.
    """
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else executable
    package_parent = Path(__file__).resolve().parent.parent
    # -m needs the package's parent on sys.path; setting it explicitly means the
    # entry keeps working regardless of what directory Windows starts us in.
    return (
        f'"{runner}" -c "import sys; sys.path.insert(0, r\'{package_parent}\'); '
        f'import mbt.__main__ as m; sys.exit(m.main([\'tray\']))"'
    )


def is_enabled() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except (OSError, FileNotFoundError):
        return False


def enable() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key, VALUE_NAME, 0, winreg.REG_SZ, _command_for_registry()
            )
        return True
    except OSError:
        return False


def disable() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except (OSError, FileNotFoundError):
        return False


def toggle() -> bool:
    """Flip the setting. Returns the state afterwards."""
    if is_enabled():
        disable()
        return False
    enable()
    return is_enabled()


__all__ = ["disable", "enable", "is_enabled", "toggle"]
