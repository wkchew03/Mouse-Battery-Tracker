"""ZOWIE presence entry, from enumeration captured on a U2-DW (04a5:800a).

There is no protocol to test here -- the device answers nothing. What matters
is that the right collection is claimed, that a BenQ display can never be
mistaken for a mouse, and that no percentage is ever invented.
"""

from mbt.drivers import zowie
from mbt.drivers.base import Reading, describe_device, resolve_label

SERIAL = "000000000081"


def _info(usage_page, usage=0, interface=1, product_id=0x800A, serial=SERIAL):
    return {
        "vendor_id": zowie.VENDOR_ZOWIE,
        "product_id": product_id,
        "usage_page": usage_page,
        "usage": usage,
        "interface_number": interface,
        "path": f"zowie-{usage_page:04x}".encode(),
        "serial_number": serial,
        "manufacturer_string": "BenQ ZOWIE",
        "product_string": "BenQ ZOWIE Gaming Mouse",
    }


# The three collections the U2-DW actually enumerates.
MOUSE = _info(0x0001, usage=0x02, interface=0)
COMMAND = _info(zowie.USAGE_PAGE_COMMAND)
EVENT = _info(zowie.USAGE_PAGE_EVENT)

DRIVER = zowie.ZowieDriver()


def test_claims_the_command_collection_once():
    """One entry per physical device, and it is the 0xff03 channel -- not the
    mouse collection and not the 0xff04 input collection."""
    assert DRIVER.candidates([MOUSE, COMMAND, EVENT]) == [COMMAND]


def test_a_benq_device_that_is_not_a_mouse_is_ignored():
    """0x04A5 is BenQ, whose monitors also expose vendor HID collections.
    Without the mouse-collection requirement a display lands in the mouse list.
    """
    assert DRIVER.candidates([COMMAND, EVENT]) == []


def test_a_mouse_without_the_vendor_collection_is_ignored():
    assert DRIVER.candidates([MOUSE]) == []


def test_collections_are_grouped_per_device():
    """Two ZOWIE mice plugged in at once stay two entries, not a cross-matched
    pair -- the mouse collection of one must not vouch for the other."""
    other_mouse = _info(0x0001, usage=0x02, interface=0, product_id=0x800B, serial="000000000099")
    other_command = _info(zowie.USAGE_PAGE_COMMAND, product_id=0x800B, serial="000000000099")

    claimed = DRIVER.candidates([MOUSE, COMMAND, other_mouse, other_command])
    assert claimed == [COMMAND, other_command]

    # ...and a second device that is command-only is still not claimed.
    assert DRIVER.candidates([MOUSE, COMMAND, other_command]) == [COMMAND]


def test_read_reports_presence_without_a_percentage():
    """The whole point: connected, with no number invented for a device that
    has never reported one."""
    reading = DRIVER.read(COMMAND)
    assert reading == Reading(online=True, bucket=zowie.NO_BATTERY)
    assert reading.percent is None
    assert reading.charging is None


def test_read_never_opens_the_device(monkeypatch):
    """Presence comes from enumeration alone. Opening a channel that answers
    nothing would cost a handle every poll for no information."""
    import mbt.hidio as hidio

    def boom(_path):
        raise AssertionError("zowie.read must not open the device")

    monkeypatch.setattr(hidio, "open_path", boom)
    assert DRIVER.read(COMMAND).online is True


def test_describe_is_readable_in_the_tray():
    assert DRIVER.read(COMMAND).describe() == "no battery data"


def test_label_names_the_model():
    """The USB strings say only "BenQ ZOWIE Gaming Mouse", which every ZOWIE
    reports, so the model name has to come from the product id."""
    assert resolve_label(DRIVER, COMMAND) == "ZOWIE U2-DW"


def test_unknown_model_keeps_the_usb_strings():
    unknown = _info(zowie.USAGE_PAGE_COMMAND, product_id=0x1234)
    assert DRIVER.label(unknown) is None
    assert resolve_label(DRIVER, unknown) == describe_device(unknown)
