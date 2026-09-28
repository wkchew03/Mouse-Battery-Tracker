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
- **Shelf card names in a narrow window.** At the minimum window width, long
  names wrap to two lines and run into the level below them ("Razer Viper V3
  Pro" over "85%"). Fine at the default width. Deferred on 2026-09-28; the
  likely fix is one line truncated with "…".
- **HUD panel spacing.** The connected-mouse panel has a large empty band
  between the status line and the chart. Faithful to the approved design, but
  more noticeable at real size.

- **Removing a placeholder or adoption.** "Add mouse" can add entries to
  `adopted.json` but nothing removes one, and no
  mouse record can be forgotten from the UI either. Delete the entry from the
  file by hand.

## Not yet verified

- **"Add mouse" in the running app.** The scan was run on real hardware (found
  the Viper as tracked and the SINO WEALTH keyboard as unrecognised, since
  keyboards expose a mouse collection too), and every step of the overlay was
  screenshotted in a preview with a faked scan result. The full click-through,
  scan on the poll thread and a placeholder then appearing, has not been done.
  "Try known protocols" has never been run against real hardware: it sends
  other vendors' commands, so it was left for the user to start.

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
- **"Add mouse" is a pill in a fixed header above the shelf**, beside the
  mouse count, so it is reachable without scrolling. It started as a card at
  the end of the shelf, which a long shelf pushed out of view.
- **"Add mouse" is an overlay on the HUD canvas, not a second window**: a
  scrim and a frosted card, one step at a time (scan, results, confirm,
  protocol results, name). Buttons are canvas-drawn, so keyboard use is
  hand-built: Tab/Shift-Tab move a focus ring, Enter activates, Escape steps
  back.
- **Prism HUD.** Chosen from three colour treatments. Connected mouse pinned
  left, every other mouse on a scrolling shelf with no scrollbar, a discharge
  chart on the connected mouse. The alert settings moved behind the gear rather
  than being dropped.
- **Animated background is panned, not frame-looped.** A frame loop would cost
  about 130 MB of `PhotoImage` in an app that advertises 45 MB. Measured after
  the change: 44.5 MB. The timer idles at 700 ms while the window is hidden.
  That figure is the app with the window never opened (43.9 MB re-measured
  2026-09-29). Opening the window takes it to about 77-85 MB, and it stays
  there after hiding: the sprites and field are kept, not freed. True of the
  last commit before "Add mouse" too; the "Add mouse" overlay adds ~13 MB only
  while it is open.
- **Charts use the full 0-100 axis.** Scaling to fit would draw a five-point
  drop as a cliff.
- **Ages are in days all the way up** ("23d ago"), never weeks, at the user's
  request. History cards show age and level; the old list view also showed the
  absolute timestamp.
- **The HUD shows every remembered mouse.** The tray menu stays capped at 6
  because it cannot scroll.
- **"Add mouse" tries known protocols only when asked, and the user is the
  check.** The scan re-detects supported mice and runs the read-only probe on
  anything unclaimed. "Try known protocols" (opt-in, behind a confirm) sends
  each driver's battery query to the unknown mouse, disguised as the model
  the driver was written for (`drivers/adopted.py`), and shows every answer.
  Nothing is kept until the user says it matches the vendor's software: a
  plausible wrong number is the failure this project has shipped twice. The
  fallbacks are a placeholder ("no battery data") and the probe report for
  writing a real driver, which then claims the device ahead of both.
- **Alias migration runs after every poll.** Logitech's unit id is only known
  after a live read, so checking once at startup would leave a mouse split
  under its old key.
