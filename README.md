# Mouse Battery Tracker

A lightweight Windows tray app that reads gaming mouse battery levels directly
over USB HID, so you don't have to open Razer Synapse / G HUB / a vendor web
configurator just to check a percentage.

Shows the currently connected mouse and recently used mice, with the last known
level and how long ago each was seen.

- **Read-only.** It never writes settings, and it opens/closes the device around
  each read, so it coexists with vendor software rather than fighting it.
- **Light.** ~33 MB RAM and effectively 0% CPU between polls (default: 60s).

## Install

Requires Python 3.10+. On the development machine `hidapi` was already present;
otherwise:

```bash
python -m pip install -r requirements.txt
```

## Use

Run the tray app:

```bash
python -m mbt tray
```

Preview the UI without any hardware:

```bash
python -m mbt tray --mock --interval 5
```

Print battery for every detected mouse and exit:

```bash
python -m mbt read
```

To run it without a console window, launch it with `pythonw.exe` instead of
`python.exe`.

## Adding support for a mouse

Discovery first — this prints every HID collection, highlighting the
vendor-defined ones where battery protocols live:

```bash
python -m mbt probe --only-known --descriptors
```

`--descriptors` reads each vendor collection's HID report descriptor, which
tells you exactly which report IDs the device supports and their payload sizes.
That is usually enough to avoid guessing at an undocumented protocol.

To read (never write) the declared feature reports and hexdump them:

```bash
python -m mbt probe --only-known --read-features
```

Then add a driver under `mbt/drivers/` implementing `candidates()` and `read()`
— see `mbt/drivers/base.py` for the protocol, and `docs/protocols.md` for the
per-vendor byte layouts and what has actually been verified on hardware.

**Match the full `(vendor_id, product_id, interface_number, usage_page, usage)`
tuple.** Every one of these mice exposes 3-6 collections and only the
vendor-defined one answers commands; matching on vid/pid alone opens the wrong
collection and times out.

## Status

| Driver | Protocol source | Verified on hardware |
|---|---|---|
| Logitech (`046d`) | HID++ 2.0, features `0x1004` / `0x1000` | not yet |
| Razer (`1532`) | 90-byte report, class `0x07` id `0x80` | not yet |
| Pulsar (`3554`) | 17-byte frames, report ID 8, cmd `0x04` | not yet |
| CompX gen 2 (`373e`) | 64-byte feature reports, opcode `0x83` | not yet |
| IPI Float 88 (`372e:1014`) | unresolved | n/a |

Frame construction and response parsing are unit-tested against the documented
layouts (`python -m pytest`), but each driver still needs checking against a
real mouse — compare `python -m mbt read` with what the vendor software reports.

The IPI Float 88 accepts writes but never answers on either vendor collection;
see `docs/protocols.md` for everything that was tried and the remaining leads.

## Tests

```bash
python -m pytest
```
