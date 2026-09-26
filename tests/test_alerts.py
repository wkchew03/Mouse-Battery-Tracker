"""Low-battery notification rules and autostart command construction."""

from unittest.mock import patch

from mbt import autostart
from mbt.drivers.base import Reading
from mbt.store import Store
from mbt.tray import LOW_BATTERY_CLEAR, LOW_BATTERY_THRESHOLD, TrayApp


class FakeIcon:
    """Stands in for pystray.Icon.

    Constructing a real Icon registers a Win32 window class, and doing that once
    per test collides with "Class already exists". Nothing here needs a real
    tray, so the class is patched out entirely.
    """

    def __init__(self, *args, **kwargs):
        self.notifications = []
        self.icon = kwargs.get("icon")
        self.title = kwargs.get("title", "")
        self.menu = kwargs.get("menu")

    def notify(self, message, title=None):
        self.notifications.append((title, message))

    def update_menu(self):
        pass

    def stop(self):
        pass


def _app(tmp_path):
    store = Store(directory=tmp_path)
    with patch("mbt.tray.pystray.Icon", FakeIcon):
        return TrayApp(store, provider=lambda: [], poll_interval=1)


def _online(percent, charging=False, key="dev", label="Mouse"):
    return [(key, label, Reading(online=True, percent=percent, charging=charging))]


def test_warns_once_below_threshold(tmp_path):
    app = _app(tmp_path)
    app._check_low_battery(_online(LOW_BATTERY_THRESHOLD - 5))
    assert len(app.icon.notifications) == 1

    # Still low on the next poll -- must not warn again.
    app._check_low_battery(_online(LOW_BATTERY_THRESHOLD - 6))
    assert len(app.icon.notifications) == 1


def test_no_warning_above_threshold(tmp_path):
    app = _app(tmp_path)
    app._check_low_battery(_online(LOW_BATTERY_THRESHOLD + 1))
    assert app.icon.notifications == []


def test_hysteresis_requires_real_recovery(tmp_path):
    """Recharging just past the threshold must not re-arm the warning."""
    app = _app(tmp_path)
    app._check_low_battery(_online(10))
    assert len(app.icon.notifications) == 1

    # Between threshold and clear: still considered the same discharge cycle.
    app._check_low_battery(_online(LOW_BATTERY_CLEAR - 1))
    app._check_low_battery(_online(10))
    assert len(app.icon.notifications) == 1

    # Above the clear level, then low again -- that is a new cycle.
    app._check_low_battery(_online(LOW_BATTERY_CLEAR + 1))
    app._check_low_battery(_online(10))
    assert len(app.icon.notifications) == 2


def test_charging_clears_the_warning(tmp_path):
    app = _app(tmp_path)
    app._check_low_battery(_online(8))
    app._check_low_battery(_online(8, charging=True))
    app._check_low_battery(_online(8))
    assert len(app.icon.notifications) == 2


def test_unknown_percentage_never_warns(tmp_path):
    app = _app(tmp_path)
    app._check_low_battery([("dev", "Mouse", Reading(online=True, percent=None))])
    assert app.icon.notifications == []


def test_notification_failure_does_not_propagate(tmp_path):
    """A failed toast must not take down the poll loop."""
    app = _app(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("no notification service")

    app.icon.notify = boom
    app._check_low_battery(_online(5))  # must not raise


def test_devices_are_tracked_independently(tmp_path):
    app = _app(tmp_path)
    both = _online(5, key="a") + _online(5, key="b", label="Other")
    app._check_low_battery(both)
    assert len(app.icon.notifications) == 2


# --------------------------------------------------------------------------
# Autostart
# --------------------------------------------------------------------------


def test_registry_command_is_quoted_and_targets_tray():
    command = autostart._command_for_registry()
    assert command.startswith('"')
    assert "tray" in command
    assert "mbt" in command


def test_autostart_reports_disabled_when_key_missing():
    with patch.object(autostart, "_winreg", return_value=None):
        assert autostart.is_enabled() is False
        assert autostart.enable() is False
        assert autostart.disable() is False


def test_run_key_is_current_user_only():
    """HKCU only -- this must never touch machine-wide autostart."""
    assert "CurrentVersion\\Run" in autostart.RUN_KEY
    assert "HKEY_LOCAL_MACHINE" not in autostart.RUN_KEY


# --------------------------------------------------------------------------
# The threshold and the switch are settings now, not constants
# --------------------------------------------------------------------------


def test_notifications_can_be_switched_off(tmp_path):
    app = _app(tmp_path)
    app.store.set_notify_low(False)
    app._check_low_battery(_online(3))
    assert app.icon.notifications == []


def test_switching_notifications_back_on_re_arms(tmp_path):
    """Turning them off must not leave a mouse permanently marked as warned --
    it dropped low while nobody was listening, so it is still news."""
    app = _app(tmp_path)
    app._check_low_battery(_online(5))
    assert len(app.icon.notifications) == 1

    app.store.set_notify_low(False)
    app._check_low_battery(_online(5))
    app.store.set_notify_low(True)
    app._check_low_battery(_online(5))
    assert len(app.icon.notifications) == 2


def test_custom_threshold_is_honoured(tmp_path):
    app = _app(tmp_path)
    app.store.set_alert_threshold(40)

    # Quiet under the default 15, but this user asked to hear about 35%.
    app._check_low_battery(_online(35))
    assert len(app.icon.notifications) == 1
    assert "35%" in app.icon.notifications[0][1]


def test_clear_level_follows_the_threshold(tmp_path):
    """The hysteresis margin is relative, so a 40% threshold clears at 50 --
    a fixed 25 would have re-armed the warning while still below it."""
    app = _app(tmp_path)
    app.store.set_alert_threshold(40)
    app._check_low_battery(_online(30))
    assert len(app.icon.notifications) == 1

    app._check_low_battery(_online(45))  # above threshold, below clear
    app._check_low_battery(_online(30))
    assert len(app.icon.notifications) == 1

    app._check_low_battery(_online(app.store.alert_clear + 1))
    app._check_low_battery(_online(30))
    assert len(app.icon.notifications) == 2


# --------------------------------------------------------------------------
# Legacy identity migration -- checked every poll, not just at startup,
# because some identities (Logitech's unit id) are only knowable after a
# device has actually answered once.
# --------------------------------------------------------------------------


def test_poll_once_folds_a_newly_resolved_identity(tmp_path):
    app = _app(tmp_path)
    app.store.update("old:1", "Mouse", Reading(online=True, percent=50))
    app.store.set_display_name("old:1", "My Named Mouse")

    with patch("mbt.app.legacy_aliases", return_value={"old:1": "new:1"}):
        app.provider = lambda: [("new:1", "Generic Label", Reading(online=True, percent=51))]
        app.poll_once()

    assert "old:1" not in app.store.records
    assert app.store.display_name("new:1") == "My Named Mouse"
