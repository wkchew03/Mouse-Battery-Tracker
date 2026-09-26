# Battery protocol notes

Working reference for each vendor. Anything marked **verified on hardware** was
confirmed on this machine; everything else comes from public reverse-engineering
work and still needs checking against a real device.

## How to pick the right HID collection

Every mouse here exposes 3-6 HID collections. Only the vendor-defined one
(usage page `0xff00`-`0xffff`) answers protocol commands. Matching on vid/pid
alone will open the mouse collection and then time out forever. Always match the
full `(vendor_id, product_id, interface_number, usage_page, usage)` tuple.

Windows blocks reading input reports from Generic Desktop mouse/keyboard
collections, so a "liveness check" by watching for movement is not possible —
`open_path` succeeds but `read` raises `read error`. Only vendor collections can
be read.

## Transport

Use **hidapi**, never pyusb. On Windows pyusb requires replacing the device's
HID driver with WinUSB via Zadig, which stops the mouse working as a mouse.
hidapi goes through `hid.dll` with shared access and coexists with vendor
software. Open, exchange, close immediately — never hold a handle between polls.

The installed package is cython-hidapi (`hid.device()`, `open_path`,
`send_feature_report`, `get_feature_report`, `write`, `read`,
`get_report_descriptor`). Note this is *not* the `hid.Device` API of the other
similarly-named PyPI package.

`get_report_descriptor()` is the single most useful discovery call: it reports
exactly which report IDs a device supports and their payload sizes, which
removes the guesswork from an undocumented device.

## CompX family (Pulsar, IPI, Attack Shark, VXE, Lamzu, Zaopin)

These brands share OEM firmware lineage, but there are **two distinct protocol
generations**, and they are easy to confuse.

### Generation 1 — VID `0x3554`, report ID `8`, short frames

Used by Pulsar and by IPI's older mice (e.g. IPI STAY FLY, `3554:f517`).

- 16-17 byte frames, report ID `0x08`
- Checksum is the last/first byte: `(K - sum(body)) & 0xff`
  - Pulsar: `K = 0x55`
  - IPI Stay Fly: `K = 0x4D`, over `byte[2..=15]`, stored in `byte[0]`
- Pulsar battery command `0x04` (POWER); response:
  `percent = resp[6]`, `power_connected = bool(resp[7])`,
  `millivolts = int.from_bytes(resp[8:10], "big")`
- Pulsar PIDs: `0xf507` (wired), `0xf508` (2.4GHz dongle)
- Some devices report cell voltage only, mapped to a percentage through a
  Li-Ion discharge curve rather than reporting percent directly

References: [python-pulsar-mouse-tool](https://github.com/andrewrabert/python-pulsar-mouse-tool)
(pyusb — protocol is correct, transport is not usable on Windows),
[IPI-Stay-Fly-Driver](https://github.com/SpookyyQ/IPI-Stay-Fly-Driver)
(Rust/hidapi, `src-tauri/src/protocol.rs`; also ships a WebHID sniffer in `tools/`).

### Generation 2 — 64-byte feature reports, no checksum

Used by Attack Shark R6/R8/R5/M5 (VID `0x373E`).

- 64-byte command buffer sent as a feature report with report ID `0`
- Battery command: `buf[2]=2, buf[3]=2, buf[4]=0, buf[5]=131 (0x83)`
- Response read back with `get_feature_report(0, 65)`
- Success marker `0xA1` at `resp[1]`; then `resp[4]==2` and `resp[6]==131`
- Battery at `resp[7]`/`resp[8]` — firmware varies on which is percent and
  which is the charging flag, so normalise: the one in `0..100` is the percent,
  the one in `(0,1)` is the charging flag
- No checksum. 50 ms delay between send and read; retry up to 3x on a missing
  `0xA1` marker
- Collection scoring: usage page `0xFF00` +100, usage `0x01` +50, lower
  interface number preferred

Reference: [attack-shark-r6-cli](https://github.com/mohammed-just/attack-shark-r6-cli) (`r6ctl.py`).

### IPI Float 88 — `372e:1014` (dongle) / `372e:1015` (wired) — SOLVED

**Verified on hardware.** `python -m mbt read` reports the same percentage as
IPI's own configurator. Implemented in `drivers/ipi.py`.

Battery is **field 5 of `get_basic_info`** — there is no dedicated battery
call, which is why grepping the vendor bundle for `get_battery` never found it.
The device answers commands normally; an earlier claim here that it "never
answers, only pushes" was wrong on both counts.

Wired, the mouse enumerates separately as `372e:1015`, manufacturer "BYTECH",
product "PIAO", with an identical collection layout.

**Why this took so long to find:** the status heartbeat is emitted roughly every
30 seconds, and the first passive listen was only 12 seconds long. It reported
"no unsolicited reports", which sent the investigation down a dead end of ~20
speculative command writes, none of which the device ever answered. *When
listening for a heartbeat of unknown period, listen for minutes, not seconds.*

#### Notification format (`0xff06`, report ID `0x09`, input, 31 B)

```
09 fa <type> <payload...>   zero-padded to the full report length
   ^^ constant marker, analogous to Attack Shark's 0xA1
```

| type | payload | meaning |
|------|---------|---------|
| `0x03` | `<index> <value LE16>` | DPI change. `09 fa 03 01 20 03` = stage 1, `0x0320` = 800 DPI |
| `0x05` | `<value>` at byte 3 | periodic status, ~every 30 s. Observed `0xe4` = 228 |

Type `0x05` is the battery candidate but is **not yet confirmed to be battery
at all**. Measured wired, it was byte-identical across 5 consecutive samples at
exactly 30 s intervals over 4 minutes:

```
01:17:30  01:18:00  01:18:30  01:19:00  01:19:30   ->  228 every time
```

Candidate interpretations of 228:

- percentage on a 0-255 scale -> 89%
- cell voltage at 16 mV/unit -> 3.648 V
- a fixed status code that has nothing to do with charge

The complete absence of jitter argues mildly against a raw voltage ADC, which
would normally drift by a count or two. Disambiguating needs a sample at a
known, different charge level — ideally on the 2.4 GHz dongle, cross-checked
against a battery percentage from some other source.

Do not ship a driver that reports this number until that check is done: all
three interpretations produce a confident-looking value, and two of them are
wrong.

#### Status blob (`0xff00`, report ID `0x03`, feature)

Returns a constant 5 bytes, and only when at least 64 bytes are requested
(shorter lengths raise `read error`):

```
03 00 02 00 02   on the 2.4 GHz dongle
03 00 02 00 00   wired
         ^^ byte 4 = link mode (0 = wired, 2 = wireless)
```

So this report is live state, not a stub — but it carries no battery value.

#### Vendor protocol, extracted from the official web driver

The official configurator is a WebHID app at `https://shan.ipigame.cn/`. Its
bundle (`/app-Cn5AWPhI.js`, ~4.5 MB) contains the complete device table and
protocol. Fetch it same-origin from the page's console and search it.

Device table entries for this mouse (`14126` = `0x372e`):

```js
{ product_name: "IPI_PIAO", vendor_id: 14126, product_id: 4117,  // 0x1015 wired
  report_id_tx: 3, report_id_rx: 3, report_id_io_rx: 3,
  send_payload_length: 63, type_protocol: "ms_pix_v1",
  type_channel: "feature_report", type_connection: "wired" }
{ ... product_id: 4116, ... type_connection: "wireless" }        // 0x1014 dongle
```

`ms_pix_v1` is handled by class `wa`. **Frame format (verified against the
vendor's own precomputed constants):**

```
payload = [crc, 0x50, a, b, opcode, params...]  zero-padded to 63 bytes
crc     = sum(payload[1:]) % 256                stored at payload[0]
send    = sendFeatureReport(report_id_tx=3, payload)
recv    = receiveFeatureReport(3) -> drop the report ID byte
```

Checked against the bundle's own literals:

| builder | bytes | sum of `[1:]` | matches `[0]` |
|---|---|---|---|
| `pct` (get uuid) | `152,80,0,1,71,0` | 152 | yes |
| `gct` | `233,80,0,10,79,64` | 233 | yes |
| `fct` | `33,80,0,2,79,128` | 289 % 256 = 33 | yes |

Reads use a 20 ms delay then `_receive_feature()`, retried via `_fetch_resp`
(up to 3 attempts, 50 ms apart, matching on `payload[3] === response[3]`).

Class `wa` has no `get_battery` method — battery rides along in
`get_basic_info` instead:

```python
payload    = [crc, 0x50, 0x00, 0x02, 0x4F, 0x81]   # padded to 63
payload[0] = sum(payload[1:]) % 256
send_feature_report([0x03] + payload)
# re-read until the reply exceeds the 5-byte stub
battery = response[6]        # vendor's a[5]; raw[0] is the report ID
```

**The retry is essential.** The first `get_feature_report` after sending
returns a 5-byte status stub (`03 50 02 00 02` — byte 1 echoes the command
marker). The real 16-byte payload appears only on a later read. Reading once
and giving up is indistinguishable from an unresponsive device, and that single
mistake is what stalled this investigation for ~25 failed command attempts.

Observed reply, with the mouse at 80%:

```
03 50 00 0b 02 01 50 21 07 01 90 65 ...
                  ^^ battery = 0x50 = 80
```

Offsets cross-checked via `max_dpi = raw[10] + raw[11] * 256` = **26000**,
matching the 26K sensor. (The vendor's own code multiplies by 255 here, an
off-by-one that yields 25899.)

#### Charging: the battery field becomes a sentinel

While the mouse is on external power, `raw[6]` reads **`0xE4` (228)** instead of
a percentage. It carries no level information.

Evidence it is a sentinel rather than `0x80 | percent`:

- The same `0xE4` appeared while the mouse sat wired at roughly 80-85%. A
  charging bit over a real level would have read `0xC4` there, and `0xE4` only
  at 100%.
- It held at exactly 228 across repeated polls spanning minutes of charging.
- **IPI's own web configurator displays "228" as the battery level while
  charging** (user-confirmed). Consistent with the bundle: a search found no
  clamping, masking, or `> 100` handling anywhere near the battery value.

So no percentage is obtainable while charging — not by us, and not by the
vendor's software either. `drivers/ipi.py` maps the sentinel to
`Reading(online=True, charging=True, percent=None)`, and the UI falls back to
the last stored level labelled "last known".

Note the mouse charges **without leaving 2.4 GHz mode**: the `0xff00` status
blob still reports link mode `02` while the cable is in, and the wired product
id `0x1015` does not appear.

**Regression risk:** an earlier version rejected any value outside 0-100 as
invalid and returned `OFFLINE`, which made a charging mouse vanish from
"Connected" entirely. A validated frame means the device answered, so it must
be reported as present even when the level is unusable.

#### Keyboard battery notification (different protocol family)

For `kb_by_v3` keyboards the driver parses a pushed notification. After the
report ID is stripped:

```
n[0] == 0xFE, n[1] == 0x05
n[2] = battery percent
n[3] = status: low nibble = full, high nibble = charging
```

Not observed on this system: a 70 s listen on the Bridge75 HE keyboard
(`0416:7372`, usage page `0xff1b`) captured nothing — it is wired.

Note the mouse's pushed reports use marker `0xFA`, not `0xFE`, and its type-5
payload (228) is not a valid percentage, so the two formats are **not**
interchangeable.

#### Implications for the driver

This device polls on demand like every other, so it keeps the standard
"open, read, close" pattern — no listener thread, no held handle. An earlier
draft of this document concluded the opposite; that was based on the failed
command attempts and is superseded.

The unsolicited `0xFA` notifications are **not** used for battery. IPI's own
driver discards the type `0x05` report, and its value (228) is not a valid
percentage.

#### Commands that do NOT work (do not retry)

Roughly 20 speculative writes were sent and every one was accepted at the USB
level (`send_feature_report`/`write` both return 64) yet produced no reply and
no change in the status blob:

- Attack Shark gen-2 opcode `0x83` at five byte alignments, on both collections
- Generation-1 checksummed frames, bases `0x55` and `0x4D`, 17-byte and 64-byte
- Pulsar `0x04` POWER, several alignments

### Historical note — the original UNRESOLVED entry

**Verified on hardware.** Enumerates as manufacturer "Gaming Mouse 8K",
product "PIAO-2.4G", serial `000000000001` (a placeholder — every unit reports
it, so it must not be used as an identity key).

Collections:

| if | usage page | usage  | reports |
|----|-----------|--------|---------|
| 0  | `0x0001`  | `0x0002` | mouse |
| 1  | `0x0001`  | `0x0006`/`0x0080`, `0x000c` | keyboard / system / consumer |
| 2  | `0x000c`  | `0x0001` | consumer |
| 2  | **`0xff00`** | `0x0001` | report `0x03`, **feature 63 B** |
| 2  | **`0xff06`** | `0x0001` | report `0x09`, **input 31 B / output 63 B** |

Raw descriptors (verified against the parser):

```
0xff00: 06 00 ff 09 01 a1 01 85 03 09 01 15 00 25 ff 75 08 95 3f b1 02 c0
0xff06: 06 06 ff 09 01 a1 01 85 09 09 03 15 00 26 ff 00 75 08 95 1f 81 02
        09 02 15 00 26 ff 00 75 08 95 3f 91 02 c0
```

What was tried, all **read-only or the documented `0x83` read opcode only**:

- `get_feature_report(0x03, ...)` returns a constant 5-byte blob
  `03 00 02 00 02`, and **only** for a requested length >= 64; shorter lengths
  raise `read error`. The blob never changes.
- `send_feature_report` on `0xff00` returns 64 (the write is accepted), but the
  blob afterwards is byte-identical.
- `write()` of a correctly-sized 64-byte output report on `0xff06` returns 64
  for every command alignment tried; `read(32, 800..2000)` never returns
  anything.
- No unsolicited input reports over a 12 s listen on either vendor collection.
- `get_input_report` raises `read error` on both collections.

Command alignments tried on both collections (Attack Shark `0x83` opcode at
various offsets): same-wire-offsets, shifted left, shifted right, opcode-first,
and with a leading `0xA1` marker. Then generation-1 style frames *with* a
checksum: 17-byte and full-64-byte variants, checksum bases `0x55` and `0x4D`,
on both collections. None produced any response, and the `0xff00` blob was
byte-identical after every single attempt.

### Two hypotheses tested and disproved

1. **"The mouse is asleep behind the dongle."** Disproved — the probe was
   re-run immediately after the mouse was clicked and moved, and the response
   was byte-identical. The device is genuinely unresponsive to these commands,
   not absent.
2. **"The frames are rejected for a bad checksum."** Disproved — adding valid
   generation-1 checksums (both known bases, both frame lengths) changed
   nothing.

So the dongle accepts writes at the USB level and simply does not implement any
of the command formats tried. ~20 distinct attempts in total.

**Conclusion: blind protocol guessing has failed and should not be continued.**
Further speculative writes cost hardware risk for no information.

The remaining reliable path is observation rather than guessing:

1. **Get the official web configurator URL** (usually a QR code or card in the
   mouse's box, or on the IPI product page). Its protocol lives in the page's
   JavaScript, which can be read statically — no device or capture needed. This
   is by far the cheapest remaining option.
2. Capture the configurator's WebHID traffic live. The IPI-Stay-Fly-Driver repo
   ships a sniffer extension in `tools/`.
3. USBPcap + Wireshark capture of the vendor software. Requires a driver
   install and admin rights.

Also untested and cheap: connect the Float 88 **by USB cable**. Wired mode often
enumerates under a different VID/PID (possibly `3554:…`) and may expose the
generation-1 protocol, which is already implemented in `drivers/pulsar.py`.

## Finalmouse — VID `0x361D`

Not to be confused with the Starlight-12 (`1915:f6b0`, Nordic's vid), which is
genuinely unreadable. The UltralightX is a different device on Finalmouse's own
vid and it has a proper vendor collection.

Product ids come from Finalmouse's published udev rules
(`github.com/teamfinalmouse/xpanel-linux-permissions`): `0100 0101 0102 0103
0104 0111 0200 0201 0202 0203`, plus `1fc9:0021` (NXP) for the bootloader.

### Wire format

Vendor collection is usage page `0xff00`. The report descriptor declares four
reports and **no feature reports**:

```
report id 0x02  output=63B     report id 0x03  input=63B
report id 0x04  output=63B     report id 0x05  input=63B
```

The ULX uses `0x04` out / `0x05` in; the SLX uses `0x01` out. Requests:

```
[0]   report id (0x04)
[1]   2 + len(args)
[2]   0x80 | command      <- the high bit marks a request
[3]   len(args)
[4:]  args, zero padded to 63 bytes
```

Replies echo the command with the high bit clear, at `[2]`, with `[3]` the
argument count. **Match on the echoed command.** The dongle volunteers
`CMD_ID_RSSI` several times a second while the link is up — 381 unsolicited
reports in a 200 s capture — so "read the next report" returns link strength
and a driver that trusts it reports RSSI as a battery level.

### Commands

From XPanel's own `CMD_ID_*` enum (`xpanel.finalmouse.com`, a WebHID app; the
table is in the `BrPfhg39` chunk). 58 entries; the ones that matter here:

| id | name | reply |
|---|---|---|
| `0x00` | `CMD_ID_HELLO` | empty |
| `0x03` | `CMD_ID_ULX_GET_DPI` | u16 LE — read **1600** |
| `0x04` | `CMD_ID_ULX_GET_POLLING_RATE` | u16 LE — read **2000** |
| `0x05` | `CMD_ID_VBAT` | u16 LE millivolts — read **3956** |
| `0x0d` | `CMD_ID_RSSI` | i8 dBm, **unsolicited** — read `0xe7` = −25 |
| `0x16` | `CMD_ID_SQUAL` | sensor surface quality |
| `0x24` | `CMD_ID_LINK_STATE` | `0` = mouse not linked |
| `0x25` | `CMD_ID_BATTERY_CHARGING` | `0`/`1` |
| `0x26` | `CMD_ID_BATTERY_STATUS` | `{soc, voltage_mv LE16}` |

### What is and is not verified

Verified on a `361d:0100` dongle: `VBAT` 3956 mV, `BATTERY_CHARGING` 0, DPI
1600, polling rate 2000, `LINK_STATE` 0, and the unsolicited RSSI stream.

**`CMD_ID_BATTERY_STATUS` never answers on this dongle** — captured with
`LINK_STATE` at 1 as well as 0, so "the mouse was asleep" does not explain it;
the firmware appears not to implement the command. It is the only source of a
percentage. Its parse — `soc` at `[0]`, millivolts LE16 at `[1]` — is lifted
from the vendor's code and has never been seen against a real reply.

So a percentage is not available here. `VBAT` is, and it moves: 3956 mV on one
capture and 3964 on a later one. Presence comes from `LINK_STATE`, which is an
exchange with the dongle reporting whether the *mouse* is on the air — the
thing enumeration alone cannot tell you.

Converting volts to a percentage would need Finalmouse's discharge curve. It
is not in the XPanel bundle anywhere this search could reach, and the Pulsar
entry above is the standing reminder of what guessing one costs. The driver
reports the voltage as the level instead.

## Ninjutso Sora V2 — VID `0x1915`

Nordic's vid, shared with Orbitalworks and the Starlight-12, so the driver
requires both a Sora product id and the `0xffa0` usage page.

Product ids, from the configurator's `soraV2` table (ninjaforce.co/sorav2, a
Nuxt WebHID app): receivers `ae1c`, `ae8c`, `ae8a` (the last two are 8K);
wired `ae11`-`ae13` (black/white/pink) and `ae14`-`ae16` (same colours, 3950
sensor). The table also lists five `093a` (PixArt) ids, which are not claimed:
nothing here shows they speak the same frames.

### Wire format

The vendor collection declares feature report `0x04` (703 B, config blobs) and
`0x05` (31 B, commands). Requests are 31 zero bytes with the command at `[0]`,
`0x01` at `[3]` and an argument at `[6]`. The reply comes back on the same
feature report; in the raw 32-byte buffer (report id at `[0]`) the command is
echoed at `[1]` and the argument at `[7]`. Byte 31 is a checksum that sums for
some replies and not others; the vendor never checks it.

| cmd | arg | reply |
|---|---|---|
| `0x15` | `4` | `[9]` level %, `[10]` charging, `[12]` mouse awake |
| `0x28` | `2` | `[9..10]` LE16 wired pid of the paired mouse |
| `0x09` | `0`/`4` | firmware version, mouse/receiver, `[9..12]` |

`[12]` is the vendor's presence check: at zero it tells the user the device is
asleep. A wired Sora is shown charging regardless of `[10]`.

### What is and is not verified

Verified on an `ae1c` receiver paired to an `ae11` mouse: level `0x55` = 85%,
matching the configurator; charging 0; awake 1; paired pid `ae11`. Not seen: the
wired ids, the 8K receivers, `[10]` at 1, `[12]` at 0.

## Razer — VID `0x1532`

Not yet verified on hardware (no Razer device present during discovery).

- 90-byte report sent as a feature report on the `usage_page 0x0001 /
  usage 0x0002` collection, prefixed with report ID `0x00`
- Struct: `status, transaction_id, remaining_packets(2, BE), protocol_type,
  data_size, command_class, command_id, arguments[80], crc, reserved`
- CRC = XOR of bytes 2..87
- Battery: `command_class 0x07`, `command_id 0x80`; returns a 0-255 level to be
  scaled to a percentage. Charging status: `0x07`/`0x84`
- `transaction_id` **varies per model** (`0x1f`, `0x3f`, `0x08`, `0x00`) — take
  the per-PID value from OpenRazer rather than guessing; a wrong value yields
  plausible-looking garbage rather than an error

References: [OpenRazer](https://github.com/openrazer/openrazer),
[razer-battery-report](https://github.com/xzeldon/razer-battery-report),
[opsrzr](https://github.com/atv57/opsrzr).

## Logitech — VID `0x046D`

Not yet verified on hardware.

- HID++ 2.0 on the vendor collection: usage page `0xff00`, usage `0x0001`
  (short, 7-byte reports) or `0x0002` (long, 20-byte reports)
- Request layout: `[report_id, device_index, feature_index, (func<<4)|sw_id, params...]`
- `device_index` = `0xff` for a directly connected device, `0x01`-`0x06` for a
  Unifying/Lightspeed receiver slot
- Resolve a feature index by calling root feature `0x0000` func `0x00` with the
  2-byte feature ID, then:
  - `0x1004` UNIFIED_BATTERY — state of charge (%) plus charging state
  - `0x1000` BATTERY_STATUS — older mice, returns **coarse buckets, not a
    percentage**. Surface the bucket; do not invent a number.
- Error `0x09` (device unavailable) means the mouse is powered off — this is the
  cleanest `online=False` signal of any vendor here

Reference: [Solaar](https://github.com/pwr-Solaar/Solaar).

## Windows PnP fallback (Bluetooth LE only)

`Get-PnpDeviceProperty -KeyName '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'`
returns a battery percentage for BLE devices.

**Verified on hardware:** returns nothing for any device on this machine — it
does not work for 2.4 GHz gaming dongles. Worth keeping only as a cheap fallback
for a future Bluetooth mouse.
