# Mouse Battery Tracker

A lightweight Windows tray app that reads gaming mouse battery levels directly
over USB HID, so you don't have to open Razer Synapse / G HUB / a vendor web
configurator just to check a percentage.

- **Read-only.** It never writes settings, and it opens and closes the device
  around each read, so it coexists with vendor software rather than fighting it.
- **Light.** ~45 MB RAM and effectively 0% CPU between polls (default: 60 s).
  No GPU context, no web view, no bundled browser. The detail window's drifting
  background is one pre-rendered image being panned, so it costs a coordinate
  change per frame and only while the window is actually open.
- **Seven brands verified against their own vendor software.**

## Install

Requires Python 3.10+.

```bash
python -m pip install -r requirements.txt
```

## Use

Double-click **`Mouse Battery Tracker.bat`**, or:

```bash
pythonw -m mbt tray
```

`pythonw` rather than `python` keeps a console window from appearing. Running it
twice is harmless — the second copy detects the first and exits.

Tick **Start with Windows** in the tray menu to launch it at login.

### What you get

- **Tray icon** showing the connected mouse's percentage, with a tooltip listing
  every mouse.
- **Detail window** (left-click the icon): two columns. The connected mouse is
  pinned on the left with its level, a chart of the current discharge and
  whatever drain estimate the data supports; every mouse it remembers sits on
  the right as a shelf of cards showing its picture, last known level and how
  long ago that was. Hover the chart to read any point. The shelf scrolls by
  wheel with no scrollbar. Rename a mouse or set a custom picture by clicking
  its name or image; the gear in the panel holds the alert settings.
- **Low-battery alert**, once per discharge cycle rather than every poll. The
  threshold and whether it fires at all are set in the detail window; a warned
  mouse must climb 10 points above the threshold before it can warn again.
- **Stream Deck plugin** (optional) — see `streamdeck/`.

### Other commands

```bash
python -m mbt read
```

Print battery for every detected mouse and exit. This is the quickest way to
check whether a driver works.

```bash
python -m mbt tray --mock --interval 5
```

Preview the UI with fake devices. Note this writes the fake mice into your saved
state.

## Stream Deck

```bash
python streamdeck/build_assets.py
powershell -ExecutionPolicy Bypass -File streamdeck/install.ps1 -Restart
```

Then add the **Mouse Battery** action to a key or dial. Keys show the gauge and
percentage; the Plus dial cycles mice on its LCD strip.

The plugin is dependency-free — Stream Deck ships its own Node runtime, and it
only ever reads the feed the tray app publishes to
`%APPDATA%/MouseBatteryTracker/streamdeck/`. All device access stays in the
Python app, because two processes polling the same mice would contend for the
HID handles.

## Adding support for a mouse

Discovery first — this prints every HID collection, highlighting the
vendor-defined ones where battery protocols live:

```bash
python -m mbt probe --descriptors --read-features
```

`--descriptors` reads each vendor collection's HID report descriptor, which
tells you exactly which report IDs the device supports and their payload sizes.
`--read-features` reads (never writes) the declared feature reports. On some
devices that alone returns the battery.

Then add a driver under `mbt/drivers/` implementing `candidates()` and `read()`
— see `mbt/drivers/base.py` for the protocol and `docs/protocols.md` for the
per-vendor byte layouts.

Three things that cost real time on this project:

- **Match more than vid/pid.** Every one of these mice exposes 3–6 collections
  and only one answers. The distinguishing signal varies: usually a
  vendor-defined usage page, but on the CRDRAKO it is *which collection declares
  a feature report*.
- **Listen for minutes, not seconds.** A device that seems silent may be on a
  30-second heartbeat. A 12-second listen produced a false negative that sent
  the IPI investigation down a dead end for ~25 speculative writes.
- **If the vendor software is web-based, read its JavaScript.** Five of the seven
  protocols came straight out of a WebHID bundle. That is far faster and safer
  than guessing at command bytes.

## Status

| Driver | Protocol | Verified against |
|---|---|---|
| Logitech (`046d`) | HID++ 2.0, feature `0x1004` | G Pro X Superlight 2 — Onboard Memory Manager |
| Razer (`1532`) | 90-byte report, class `0x07`/`0x80` | Viper V3 Pro — Synapse |
| Pulsar (`3710`, `3554`) | 17-byte frames, report 8, cmd `0x04` | X2N + TenZ — bbb.pulsar.gg |
| Hitscan (`3770`) | same platform as Pulsar | Hyperlight — Hitscan Utility |
| IPI (`372e`) | `get_basic_info`, report `0x03` | Float 88 — shan.ipigame.cn |
| Orbitalworks (`1915`) | 64-byte reports, cmd `0x81` | Pathfinder V1 — orbital-web-ctrl |
| VAXEE (`3057`) | feature report `0x0e`, cmd `0x0b`, 0-20 step | XE-S Wireless — VAXEE Control Center |
| Ninjutso (`1915`) | feature report `0x05`, cmd `0x15` | Sora V2 — ninjaforce.co configurator |
| Finalmouse (`361d`) | 64-byte reports, `0x80`-marked commands | UltralightX — XPanel (voltage, charging, DPI, polling rate) |
| CompX gen 2 (`373e`, `33e4`) | 64-byte feature reports, opcode `0x83` | CRDRAKO KO-ONE, G-Wolves HTS Ultra |

Every percentage above was cross-checked against the vendor's own software, not
just "the driver returned a number". That distinction caught two bugs that a
plausible-looking reading would have hidden.

### Detected, but no battery level

- **ZOWIE U2-DW** (`04a5:800a`) — appears in the tray and the HUD as *no
  battery data*. It has a vendor channel (`0xff03` out / `0xff04` in) but never
  answers and never volunteers anything, across ~5 minutes of passive capture,
  and there is no vendor software or public protocol to copy from. Presence
  therefore comes from USB enumeration alone: unlike every other driver, it
  cannot tell an awake mouse from a receiver left plugged in with the mouse
  switched off, so "last active" tracks the dongle rather than the mouse.
  Remaining lead: capture its firmware update tool.

### Not supported

- **Finalmouse Starlight-12** (`1915:f6b0`) — not possible. Its receiver's
  entire report descriptor is 64 bytes of plain mouse: no vendor collection, no
  feature or output reports, nowhere to send a query. Note this is Nordic's vid
  and says nothing about the UltralightX, which is on Finalmouse's own `361d`
  and is supported.

### A note on Pulsar / Hitscan percentages

Neither vendor's software displays the level byte the receiver reports. Both
derive the figure from cell voltage via a lookup table that treats anything
above 4110 mV as 100%, and in both cases the level byte disagreed with what the
vendor showed:

| device | level byte | voltage | vendor software |
|---|---|---|---|
| Pulsar X2N | 95% | 4122 mV | 100% |
| Hitscan Hyperlight | 70% | 4176 mV | 100% |

So this app uses the voltage curve for the whole platform. Raw millivolts are
kept on the reading either way.

## Tests

```bash
python -m pytest
```

Frame construction and response parsing are unit-tested against **captured
bytes from real hardware**, so a refactor that shifts an offset fails loudly.
