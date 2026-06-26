"""
hidpp_battery_monitor.py

Direct battery monitor for the Logitech G PRO X Superlight 2 (and other
Lightspeed mice) using the HID++ 2.0 protocol. Talks straight to the mouse
or its USB receiver - does NOT need G HUB or Onboard Memory Manager to be
running. Works whether you use OMM, G HUB, or nothing at all.

Requirements
------------
    pip install hidapi

    Careful: there are two similarly-named PyPI packages. "hidapi" is the
    correct one and gives you `import hid`. The unrelated "hid" package
    will shadow it and break things if both end up installed.

How it works
------------
HID++ 2.0 is Logitech's standard wireless protocol - the same one Solaar
uses on Linux. Battery info lives behind a "feature" called
UNIFIED_BATTERY (0x1004):
  1. Open the mouse's raw HID interface directly (USB receiver or wired).
  2. Ask the ROOT feature (always index 0) where UNIFIED_BATTERY lives.
  3. Call getStatus on that feature index to read charge % + charging state.
  4. Block on a read with a 5-second timeout in a loop. This means the
     process is genuinely asleep in the OS almost all the time - it only
     wakes up when the mouse pushes an update, or every 5s to check
     whether a periodic refresh (every 60s) is due. CPU usage is ~0%.

IMPORTANT - verify your Product IDs first
------------------------------------------
Vendor ID is always 0x046D for Logitech, but Product IDs vary by hardware
revision/region. Check yours in:
    Device Manager -> your mouse -> Properties -> Details -> Hardware Ids
Look for VID_046D&PID_XXXX - once with the Lightspeed receiver plugged in
(wireless PID), and once connected via USB-C (wired PID). Update
WIRELESS_PID / WIRED_PID below if they don't match what's here.

Note on running alongside OMM: OMM talks to the mouse the same way this
script does. Running both at once is fine, but if you see an occasional
garbled read, it's harmless - this script discards anything that doesn't
match the exact response it expects.
"""

import time
from pathlib import Path
import json

import hid

VENDOR_ID = 0x046D
WIRELESS_PID = 0xC54D  # Lightspeed receiver - PRO X Superlight 2 family
WIRED_PID = 0xC09B      # Direct USB-C - PRO X Superlight 2 family

UNIFIED_BATTERY_FEATURE = 0x1004
SW_ID = 0x0A  # arbitrary tag (0-15) used to match requests to responses

STATUS_FILE = Path.home() / ".mouse_battery_status.json"

CHARGE_STATUS = {0: "discharging", 1: "charging", 2: "charging (slow)", 3: "full"}


def find_device():
    """Try wireless (via receiver) first, then wired. Returns (handle, device_idx)."""
    for pid, device_idx, label in (
        (WIRELESS_PID, 0x01, "wireless (Lightspeed receiver)"),
        (WIRED_PID, 0xFF, "wired (USB-C)"),
    ):
        try:
            h = hid.device()
            h.open(VENDOR_ID, pid)
            print(f"Connected: {label}")
            return h, device_idx
        except OSError:
            continue
    return None, None


def get_feature_index(h, device_idx, feature_id):
    request = [0x10, device_idx, 0x00, SW_ID, (feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00]
    h.write(request)
    for _ in range(5):
        resp = h.read(20, 500)
        if resp and resp[0] == 0x10 and resp[1] == device_idx and resp[3] == SW_ID:
            return resp[4]
    return None


def get_battery_status(h, device_idx, feature_index):
    swid_byte = 0x10 | SW_ID  # function 1 (getStatus) in the high nibble
    request = [0x11, device_idx, feature_index, swid_byte] + [0x00] * 16
    h.write(request)
    for _ in range(5):
        resp = h.read(20, 500)
        if resp and resp[0] == 0x11 and resp[1] == device_idx and resp[2] == feature_index:
            return resp[4], CHARGE_STATUS.get(resp[6], "unknown")
    return None, None


def write_status(pct, status):
    STATUS_FILE.write_text(json.dumps({"percentage": pct, "status": status}, indent=2))


def run():
    while True:
        h, device_idx = find_device()
        if h is None:
            print("Mouse not found over USB HID. Check the PIDs (see header comment). Retrying in 5s...")
            time.sleep(5)
            continue

        feature_index = get_feature_index(h, device_idx, UNIFIED_BATTERY_FEATURE)
        if feature_index is None:
            print("UNIFIED_BATTERY feature not found - this mouse may need a different feature ID.")
            h.close()
            time.sleep(5)
            continue

        last_reported = None

        def report(pct, status):
            nonlocal last_reported
            if (pct, status) != last_reported:
                print(f"{time.strftime('%H:%M:%S')}  {pct}% ({status})")
                write_status(pct, status)
                last_reported = (pct, status)

        pct, status = get_battery_status(h, device_idx, feature_index)
        if pct is not None:
            report(pct, status)
        last_refresh = time.time()

        try:
            while True:
                # Blocks here almost the whole time - near-zero CPU usage.
                resp = h.read(20, 5000)
                if resp and resp[0] == 0x11 and resp[1] == device_idx and resp[2] == feature_index:
                    report(resp[4], CHARGE_STATUS.get(resp[6], "unknown"))

                if time.time() - last_refresh > 60:
                    pct, status = get_battery_status(h, device_idx, feature_index)
                    if pct is not None:
                        report(pct, status)
                    last_refresh = time.time()
        except OSError:
            print("Lost connection (unplugged / receiver removed?). Reconnecting...")
            h.close()
            time.sleep(5)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass