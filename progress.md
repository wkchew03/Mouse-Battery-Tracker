# Progress

Where the project stands, what is left, and the decisions behind the current
shape. Last updated 2026-09-28.

## Open work

- **Finalmouse percentage.** The driver shows the UltralightX as cell voltage
  ("3.96 V"), not a percentage. `CMD_ID_BATTERY_STATUS` is the only source of
  one and the dongle never answers it, with the link up or down. To get a
  percentage, either find Finalmouse's discharge curve or confirm what XPanel
  itself displays at a known voltage. See the Finalmouse section of
  `docs/protocols.md`.
- **Read-only settings.** Reading DPI and polling rate is proven on the
  Finalmouse (`CMD_ID_ULX_GET_DPI` 1600, `..._POLLING_RATE` 2000) and was
  assessed as doable for most drivers. Not started. A capability-discovery pass
  over the connected mice was offered and not yet taken up.
- **Writing settings.** Assessed, not started, and a real decision rather than
  a task: it breaks the README's lead claim that the app is read-only and
  coexists with vendor software. Logitech (HID++ `0x2201`) and VAXEE are the
  most tractable. Any write needs the command queue on the poll thread, never a
  write from the UI thread.
- **HUD panel spacing.** The connected-mouse panel has a large empty band
  between the status line and the chart. Faithful to the approved design, but
  more noticeable at real size.

## Not yet verified

- **Ninjutso Sora V2 asleep and wired.** Only the `ae1c` receiver with the
  mouse awake has been read (85%, matching the configurator). The awake flag
  at 0, whether the pairing query still answers with the mouse off (it keeps
  the entry under `ninjutso:ae11`), and the wired `ae11` form are from the
  bundle only.

- **Chart hover in the running app.** The handlers were driven directly and
  work (point readings, caption restored on leave, one card repainted). A
  screenshot never captured it, because synthetic cursor moves do not raise Tk
  motion events on an unfocused window. Worth one real mouse-over.
- **ZOWIE presence.** Reported from enumeration alone. Whether its entry
  disappears when the mouse is switched off, or stays because the receiver
  stays enumerated, was never tested.
- **Pulsar `0x3554`.** The older platform shares the frame layout with the
  verified `0x3710` dongle but has not been tested on hardware.
- **Finalmouse product ids other than `0100`.** Taken from Finalmouse's
  published udev rules, so the ids are right, but only the dongle in hand was
  exercised.

## Stored-data changes made by hand

No backups of these survive: they were written to a session scratchpad that has
since been cleared.

- Deleted the duplicate record `373e:001c:0505D08F` ("LAMZU MAYA X"). Since
  2026-09-28 `compx.py` gives `001c` and `001e` the shared identity
  `lamzu:mayax`, so the wired form folds into the dongle's entry instead of
  coming back as a duplicate. Not yet seen with the mouse actually wired.
- Removed 23 history samples from `logitech:e4cd8e68`, every one an exact 15%
  between readings of 75-87%. They were `getCapabilities` replies parsed as
  battery by the HID++ desync bug.
- Kept, at the user's choice: `373e:b01e`, a LAMZU dongle seen only in
  firmware-update mode, with `last_online` 0. Hidden from the HUD and tray;
  shown on the Stream Deck as "never".

## Diagnostics

- A driver `read()` that raises is logged to `%APPDATA%/MouseBatteryTracker/tray.log`
  (`<driver> read failed on <key>: <exc>`) before it counts as offline. A
  mouse that stays "off" while switched on: check there first.

## Decisions

- **Read-only stays.** No driver writes a setting. Every command sent is a
  query.
- **No invented percentages.** Voltage is shown as voltage (Finalmouse), absence
  as absence (ZOWIE), and the Pulsar platform uses the vendor's voltage curve
  rather than its level byte, because that is what the vendor displays.
- **Prism HUD.** Chosen from three colour treatments. Connected mouse pinned
  left, every other mouse on a scrolling shelf with no scrollbar, a discharge
  chart on the connected mouse. The alert settings moved behind the gear rather
  than being dropped.
- **Animated background is panned, not frame-looped.** A frame loop would cost
  about 130 MB of `PhotoImage` in an app that advertises 45 MB. Measured after
  the change: 44.5 MB. The timer idles at 700 ms while the window is hidden.
- **Charts use the full 0-100 axis.** Scaling to fit would draw a five-point
  drop as a cliff.
- **Ages are in days all the way up** ("23d ago"), never weeks, at the user's
  request. History cards show age and level; the old list view also showed the
  absolute timestamp.
- **The HUD shows every remembered mouse.** The tray menu stays capped at 6
  because it cannot scroll.
- **Alias migration runs after every poll.** Logitech's unit id is only known
  after a live read, so checking once at startup would leave a mouse split
  under its old key.
