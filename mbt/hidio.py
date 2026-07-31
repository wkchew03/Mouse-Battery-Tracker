"""Thin safety layer over hidapi.

The one rule this module exists to enforce: open the device, do the exchange,
close it again immediately. Never hold a handle open between polls. Holding a
handle is what causes the documented conflicts where a third-party tool stops
the vendor software (Synapse, G HUB) from reading its own devices.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import hid


class HidError(RuntimeError):
    """Transport-level failure talking to a device."""


@contextmanager
def open_path(path: bytes) -> Iterator["hid.device"]:
    """Open one HID collection by path, guaranteeing it gets closed."""
    dev = hid.device()
    try:
        dev.open_path(path)
    except Exception as exc:  # hidapi raises bare OSError/IOError
        raise HidError(f"could not open {path!r}: {exc}") from exc
    try:
        yield dev
    finally:
        try:
            dev.close()
        except Exception:
            pass


def enumerate_devices(vendor_id: int = 0, product_id: int = 0) -> list[dict]:
    """List HID collections, sorted into a stable, readable order."""
    infos = hid.enumerate(vendor_id, product_id)
    return sorted(
        infos,
        key=lambda d: (
            d.get("vendor_id") or 0,
            d.get("product_id") or 0,
            d.get("interface_number") if d.get("interface_number") is not None else -1,
            d.get("usage_page") or 0,
            d.get("usage") or 0,
        ),
    )


def report_descriptor(path: bytes) -> bytes:
    """Fetch the raw HID report descriptor for a collection.

    This is read-only and tells us exactly which report IDs a device supports
    and how large they are -- far better than probing report IDs blindly.
    """
    with open_path(path) as dev:
        raw = dev.get_report_descriptor()
    return bytes(raw)
