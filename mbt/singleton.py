"""Single-instance guard.

Two copies of the tray app means two icons, two pollers hitting the same mice,
and two writers racing on state.json. That is easy to trigger by accident --
double-clicking the launcher, or having autostart enabled and then starting it
by hand.

Uses a named mutex, which the OS releases automatically when the process dies,
so a crash can never leave a stale lock behind (unlike a pid file).
"""

from __future__ import annotations

import ctypes

# "Local\" scopes the name to the current logon session, so it does not clash
# with another user running the app at the same time.
MUTEX_NAME = "Local\\MouseBatteryTracker.singleton"
_ERROR_ALREADY_EXISTS = 183

# Held for the process lifetime; released by the OS on exit.
_handles: list[int] = []


def acquire() -> bool:
    """True if this process now owns the lock, False if another instance holds it.

    Returns True when the check cannot be performed (non-Windows, or the call
    fails) -- refusing to start because the guard itself broke would be worse
    than allowing a second instance.
    """
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return True

    try:
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except OSError:
        return True
    if not handle:
        return True

    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _handles.append(handle)
    return True
