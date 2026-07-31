"""Single-instance guard.

Every test uses a unique mutex name. The first version reused the real one,
which meant the tests failed whenever the actual tray app happened to be
running -- a test that depends on whether an unrelated process is alive is
worse than no test.
"""

import ctypes
import uuid

import pytest

from mbt import singleton


@pytest.fixture
def isolated(monkeypatch):
    """Point the guard at a mutex name nothing else can be holding."""
    monkeypatch.setattr(
        singleton, "MUTEX_NAME", f"Local\\mbt-test-{uuid.uuid4().hex}"
    )
    monkeypatch.setattr(singleton, "_handles", [])
    yield
    for handle in singleton._handles:
        ctypes.windll.kernel32.CloseHandle(handle)


def test_second_acquire_is_refused(isolated):
    """The first caller owns the lock; a second is refused.

    Mirrors the cross-process case, since the name is scoped to the logon
    session rather than to a process.
    """
    assert singleton.acquire() is True
    assert singleton.acquire() is False


def test_lock_is_released_when_the_handle_closes(isolated):
    """The OS drops the mutex with the process, so a crash leaves no stale lock."""
    assert singleton.acquire() is True
    for handle in singleton._handles:
        ctypes.windll.kernel32.CloseHandle(handle)
    singleton._handles.clear()
    assert singleton.acquire() is True


def test_name_is_session_scoped():
    """Global\\ would clash between users on the same machine; Local\\ does not."""
    assert singleton.MUTEX_NAME.startswith("Local\\")


def test_guard_failure_allows_startup(monkeypatch):
    """If the guard itself breaks, the app must still start -- refusing to run
    because the lock check failed would be worse than a second instance."""

    class Boom:
        def __getattr__(self, name):
            raise OSError("no kernel32")

    monkeypatch.setattr(ctypes, "windll", Boom())
    assert singleton.acquire() is True
