# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pytest                       # full suite (fast, no hardware touched)
python -m pytest tests/test_pulsar_8k.py -k checksum   # single test
python -m mbt read                     # one-shot battery for every detected mouse
python -m mbt probe --descriptors --read-features      # HID discovery for new devices
pythonw -m mbt tray                    # the app (pythonw = no console window)
python -m mbt tray --mock --interval 5 # UI preview, no hardware (writes fake mice into real state.json)
node streamdeck/test_plugin_logic.js   # Stream Deck plugin against the live feed
```

No linter or formatter is configured. There is no venv; packages are installed
against the system interpreter (`Mouse Battery Tracker.bat` hardcodes
`%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe`).

The user usually has the tray app running. **A code change does not reach it**
until the `pythonw.exe` process is restarted, and it rewrites `state.json` from
memory every poll, so hand-editing that file while it runs is silently undone.
To preview the HUD against real data without writing into it, point `APPDATA`
at a temp copy before importing `mbt`.

Progress, pending work and open decisions live in `progress.md`. Read it at the
start of a session.

## Architecture

Windows tray app that reads gaming-mouse battery over USB HID. Layers, bottom up:

- `hidio.py` — the only module that calls `hid`. Enforces open → exchange →
  close per read; never holds a handle between polls (that is what breaks
  Synapse/G HUB coexistence).
- `drivers/base.py` — `Reading` dataclass, the `Driver` protocol
  (`candidates()` + `read()`, optional `identity()`/`label()`, plus an optional module-level `legacy_aliases()`),
  and the identity/dedup helpers. **Read this before touching any driver.**
- `drivers/*.py` — one module per vendor, self-registering via `register()` at
  import in `drivers/__init__.py` (order = claim priority; specific before
  generic).
- `app.py` — `discover()` pairs devices with drivers (first driver to claim a
  `device_key` wins); `read_all()` polls them. Kept UI-free so `mbt read` works.
- `poller.py` — wraps `discover()` with per-device exponential backoff
  (60→120→240→300s) after 3 failures, serving the cached reading while backed
  off. `reset_backoff()` is what makes manual refresh actually re-query.
- `store.py` → `%APPDATA%/MouseBatteryTracker/{state.json,names.json,settings.json}`,
  atomic writes. `history.py` holds the pure sample/estimate functions it calls.
  Settings (alert threshold, notification switch) live in their own file so a
  checkbox does not rewrite the much larger history.
- `tray.py` (pystray) + `hud.py` (Tk) + `cards.py`/`mouseart.py`/`theme.py`
  (Pillow rendering). `feed.py` publishes JSON + pre-rendered PNGs to
  `%APPDATA%/MouseBatteryTracker/streamdeck/` for the Node plugin.
  The HUD is **one Canvas**, not a widget tree: `prism.py` renders an oversized
  colour field that is *panned* (a `coords()` call, not a re-render -- a frame
  loop would cost ~130 MB of `PhotoImage`), frosted panels and cards are RGBA
  sprites that Tk composites over it, and every string is `create_text` so it
  keeps native font rendering. Hover repaints one sprite via `_card_items` /
  `_repaint_chart`; a full `_rebuild()` on hover re-renders every card in the
  window. The shelf scrolls by moving the `shelf` tag, and the fades are what
  stand in for the scrollbar. The alert settings are a dialog behind the panel's
  gear (`_open_settings`), not inline -- the approved design has no room for them.
  "Add mouse" is an overlay drawn on the same canvas (`_paint_overlay`, one
  `view` per step), repainted at the end of every `_rebuild` so a poll landing
  mid-flow does not wipe it.
- `design/` holds the Claude Design canvas the current HUD was drawn from. The
  app never imports it; it exists to iterate on the look.

### Invariants that are easy to break

- **The tray/UI threads never touch HID.** pystray owns the main thread, Tk runs
  on its own thread and owns its widgets; the tray posts onto a queue drained by
  Tk's `after()`. `TrayApp` only consumes a `provider` callable. UI work that
  must open a device (the "Add mouse" scan, `probe.scan()`) goes through
  `TrayApp.run_on_poll_thread`, so it never races a poll.
- **Devices are matched on the full `(vid, pid, interface_number, usage_page,
  usage)` tuple** via `matches()`, never vid/pid alone — every mouse exposes 3–6
  collections and only the vendor-defined one answers. On the CompX gen-2
  platform the distinguishing signal is instead *which collection declares a
  feature report* (`compx.has_feature_channel`).
- **Identity ≠ USB device.** One mouse enumerates under different pids wired vs.
  wireless, and a dongle can be re-paired. `resolve_identity()` prefers a
  driver's `identity()`, then `Reading.device_id`, then `device_key()`;
  `collapse_duplicates()` keeps the entry that actually answered. Backoff is
  keyed by hardware, results are keyed by identity.
- **A driver's `legacy_aliases()` can depend on a live read.** IPI's is a
  static PID table, available immediately. Logitech's unit id (used to merge
  wired/wireless into one identity) is only learned by querying the device, so
  its `legacy_aliases()` is sourced from a runtime cache that is empty on a
  cold start. `TrayApp` therefore re-checks aliases after every poll, not just
  at startup — otherwise a mouse whose identity resolves after the first read
  stays split under its old key forever. `Store.merge_aliases()` combines
  history from both sides rather than picking one, so the losing key's
  drain-rate samples are not silently discarded.
- **Match a reply on something unique to its request, never on arrival order.**
  Both real failures of this kind shipped a plausible wrong number. Logitech's
  root-feature queries are byte-identical except for a field the reply does not
  echo, so a late reply resolved `0x1000` to `0x1004`'s index and
  `getCapabilities` parsed as "15%, charging" for weeks -- fixed by rotating the
  HID++ software id per request (`next_software_id`). The Finalmouse dongle
  broadcasts RSSI several times a second on the reply channel, so it matches the
  echoed command byte. Draining the queue first does not cover either case: the
  offending report is still in flight when the drain runs.
- **`Reading(online=False)` means "mouse powered off", not "read failed."** A
  dongle stays enumerated with its mouse off, so only a successful protocol
  exchange proves presence. (`adopted.py` reads a user-confirmed unknown mouse
  through a real driver, disguised as that driver's model -- see its
  docstring.) `read()` raises on transport failure so the poller
  can back off. The exceptions are `zowie.py`, which has no protocol at all,
  and the placeholders in `adopted.py` (mice the user added from the HUD's
  "Add mouse" card because no driver reads them); both report presence from
  enumeration — see
  `zowie.py`'s docstring before copying that pattern anywhere else. A dongle
  can also answer *about* a sleeping mouse: Finalmouse's `CMD_ID_VBAT` replies
  from cache, so presence there comes from `CMD_ID_LINK_STATE` instead, never from the voltage reply existing.
- **Never invent a percentage.** Coarse Logitech buckets go in `bucket`, raw
  millivolts in `millivolts`. On the Pulsar/Hitscan platform the level byte is
  deliberately ignored in favour of the voltage curve, because that is what the
  vendor software shows. `bucket` is also the slot for any level that is not a
  percentage -- ZOWIE's "no battery data", Finalmouse's "3.96 V". Converting
  volts needs the vendor's discharge curve; without it, show the volts.
- hidapi only, never pyusb (pyusb needs the HID driver swapped for WinUSB via
  Zadig, which stops the mouse being a mouse). The installed package is
  cython-hidapi — `hid.device()`, not the `hid.Device` of the other PyPI package.

## Adding or changing a driver

`docs/protocols.md` has the per-vendor byte layouts and the collection-selection
rules; each driver's module docstring records its wire format, its source
(usually the vendor's own WebHID bundle) and a **STATUS** line naming the exact
hardware it was verified on. Keep that docstring accurate — it is the only
record of what a reading was cross-checked against.

When the vendor configurator is a web app, its protocol is in the JS. Bundles
are code-split and lazily loaded, so the entry chunks the page preloads usually
contain only the bootloader or nothing; fetch every chunk the route map names
(`/_app/immutable/nodes/*` on SvelteKit) and search for `sendReport` and for a
named command enum. Finalmouse's was a `CMD_ID_*` table of 58 entries.

A parse lifted from vendor code is still unverified until a real reply has been
seen. XPanel parses `CMD_ID_BATTERY_STATUS` correctly for firmware that has it;
the dongle in hand never answers it. Write the STATUS line to say which fields
were observed and which were only read out of the bundle.

Tests are pure functions over captured real-hardware bytes: build frames, parse
synthetic replies, monkeypatch `candidates()` helpers. Nothing in `tests/` opens
a device, and new tests must not either.
