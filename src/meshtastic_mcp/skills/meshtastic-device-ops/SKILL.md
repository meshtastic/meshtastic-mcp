---
name: meshtastic-device-ops
license: GPL-3.0-only
description: Discover, connect to, drive, configure, observe, and flash Meshtastic devices through the Meshtastic MCP server. Use when an agent needs to bring up a radio (serial or TCP), read or change device/channel config, send messages, inspect the node DB, watch live packets/telemetry/logs via the recorder, recover a bricked device, or build/flash firmware. Covers the non-e2e MCP tool surface; for cross-plane device↔app testing use the `meshtastic-e2e` skill.
---

# Meshtastic device operations (via the MCP server)

The Meshtastic MCP server exposes a large tool surface, plus resources and prompts. This
skill maps common operator intents to the right tools and the safe order to use them.

## First: know your environment

- **`doctor`** (tool) or the **`meshtastic://doctor`** resource — what's installed, what's
  missing, and the exact command to acquire it. Call this first if any tool fails with a
  missing-prerequisite error, or before an e2e/flash run.
- **`meshtastic://capabilities`** resource — one line: which capability groups are active
  (`core`, `firmware`, `android`, `apple`).
- Prereq binaries are gated: `firmware` tools (build/flash/boards/userprefs) register only
  when `MESHTASTIC_FIRMWARE_ROOT` + `pio` are present. Core (admin/recorder/transport) is always on.
- The server's `MESHTASTIC_MCP_TOOLSETS` / `MESHTASTIC_MCP_READ_ONLY` can hide whole groups
  (the flash and `nrf52_*` tools live in `hardware` and `firmware`). A tool that is not listed
  cannot be called; ask the user to widen the setting rather than working around it.

## Connect

Two transports, same admin surface:
- **Serial:** a USB port from `devices` (e.g. `/dev/cu.usbmodem101`).
- **TCP:** `tcp://host:port` (a networked node, or a virtual `meshtasticd` at `127.0.0.1:4403`).

**Prefer the CLI subcommands for read-only discovery** — they cost no MCP schema tokens
and work without the server running:
```bash
meshtastic-mcp devices                  # find ports (bash, ~0 schema tokens)
meshtastic-mcp devices --all            # include non-Meshtastic serial ports
meshtastic-mcp info <port>              # firmware version, region, node identity
meshtastic-mcp nodes <port>             # mesh peers (long/short name, SNR, last-heard)
meshtastic-mcp boards                   # list all PlatformIO board envs
meshtastic-mcp boards --arch esp32s3    # filter by architecture
meshtastic-mcp boards get heltec-v3     # full metadata for one board
# All accept --json for structured output.
```

Fall back to MCP tools when already in an MCP session or when you need board metadata
for a follow-on `install`:
```
devices()                        # attached radios by name; opens no port
inspect(node=N)                  # firmware version, node identity, region, channel summary
inspect(node=N, what="nodes")    # the node DB (long/short name, SNR, hops, last-heard)
```

Pass `node` as a name `devices` reports (`heltec-v3-aa13`), the port `devices` reports (on Linux prefer the stable
`/dev/serial/by-id/...` link) or `tcp://host:port`. Omit it only when exactly one radio is
attached.

One MCP call per serial port at a time — the port lock is **exclusive and non-blocking**:
contention fails fast with "busy … Retry shortly" (it does not queue). Open → act → close.

A board whose USB serial has wedged fails the call within `timeout_s` plus a grace period,
with "looks wedged. Power-cycle it (uhubctl_cycle) and retry". Power-cycle it; retrying the
same call does not help.

## Configure (mutating)

```
inspect(node=N, what="lora")                         # a section, default values included
configure(node=N, changes={"lora.region": "US", "owner.long_name": "Base"})
configure(node=N, restore=<snapshot>)                # undo, from the snapshot it returned
inspect(node=N, what="url")                          # the shareable channel URL (keys)
set_channel_url(url=U, port=P)
```
`configure` snapshots first and reads every change back over a fresh connection, so
`verified: true` means it stuck; no restart is needed to commit. Fields the firmware changed
on its own (a region change caps `tx_power`) come back as `side_effects`. Region
(`lora.region`) and `network.enabled_protocols` are the two that bite; see `meshtastic-e2e`
`topology.md`.

## Destructive tools and confirmation

`admin` (restarts, power-off and resets), the flash tools, the `nrf52_*` writers, `uhubctl_power`
(off) / `uhubctl_cycle`, `vanity_apply` and the destructive `esptool`/`nrfutil`/`picotool` calls
take `confirm`.

- Call without `confirm` first. A client that supports elicitation shows the user
  `Allow: <action>?`; accept runs the tool, decline fails it.
- A refusal naming `confirm=True` means this client cannot ask (subagents, `claude -p`). Tell
  the user what will happen and pass `confirm=True` only after they agree. Never retry with it
  on your own.
- With `MESHTASTIC_MCP_CONFIRM=human` on the server, irreversible tools (`admin` resets,
  `install(erase=True)`, `nrf52_factory_erase`, `vanity_apply`, destructive vendor calls) ask even
  with `confirm=True`; "cannot ask" then means the user must run it from a client that can.
- `vendor(tool="esptool", args=["flash_id"])` and other harmless subcommands never ask.

## Vanity identity (chosen node id / app colour)

A node's number is `crc32(x25519_public_key)` and every app paints it with the low 24 bits
read as RGB, so both are chosen by grinding keys, not by setting a field.

```
vanity_grind_start color=crimson tol=6        # or pattern=dc80, or both; returns job_id
vanity_grind_poll <job_id>                    # hits[]; NEVER use one with verified=false
vanity_preview <private_key>                  # what any key gives you — no device, no GPU
vanity_apply <private_key> <port>             # asks; irreversible (see Destructive tools)
```

- `tol` is free and finds a hit orders of magnitude sooner — always offer it.
- Grinding needs `mvgrind` (`doctor` prints how to build it); preview/apply do not.
- `vanity_apply` **replaces the identity**, drops the old NodeNum from the node's own DB, and
  reboots the board. It needs `lora.region` set (keygen is skipped while UNSET) and refuses an
  unclamped key. Keep the previous key if a way back matters.
- Hits are private keys. Don't echo them further than needed; see `docs/vanity.md`.

## Message + observe

```
send(node=N, text="...")                         # broadcast
send(node=N, to="!c2c91376", text="...", want_ack=True)   # delivery: delivered | relayed | nak | no_ack
send(node=N, to="!c2c91376", request="traceroute")       # or "telemetry"; returns the reply
```
The **recorder** is always capturing to JSONL; query windows instead of tailing:
```
packets_window     # recent RX/TX packets (portnum, from/to, payload) — wire truth
telemetry_timeline # device/environment metrics over time
logs_window        # firmware log lines
events_window      # recorder-marked events
mark_event         # drop a labeled marker to anchor a later query
recorder_status / recorder_pause / recorder_resume / recorder_export
```
For app-visible delivery vs wire truth (broadcast shows an error icon in a flat mesh even when
delivered) see `meshtastic-e2e` `references/loop-outbound.md`.

## Recover + flash

```bash
# Board lookup — use the CLI (no schema overhead):
meshtastic-mcp boards --query <slug>                     # find the env name
meshtastic-mcp boards get <env>                          # confirm arch + upload_speed
```
Build and flash from the firmware tree (firmware capability) with `install`, which picks the
flasher from the board:
```
install(port=P, source={"env": E})              # build + flash; ESP32 OTA-updates the app
install(port=P, source={"env": E}, erase=True)  # ESP32 full erase and factory flash
install(port=P, source={"file": F})             # .uf2 (nRF52), .zip (nrfutil DFU), factory .bin
job(job_id=ID, wait_s=50)                       # follow a flash past a client timeout
```
With the server's `[tasks]` extra a task-capable client runs `install` as a background task;
otherwise the call blocks until the job ends. It returns a `job_id` and logs it before it waits.
A flash cannot be canceled: if the call is canceled or times out, follow that id with `job` to
the end before touching the board. A failed flash is a tool error carrying the output tail. A
build alone is `build(env=E)` in the opt-in `firmware` toolset.

**nRF52 by UF2** (Meshtastic bootloader; core, no firmware tree needed). Faster than serial DFU
and no touch:
```
nrf52_enter_uf2(port=P)                    # reboots into the UF2 drive (asks)
nrf52_bootloader_info(port=P)              # bootloader version, SoftDevice, Last-Fault
nrf52_backup_firmware(port=P)              # CURRENT.UF2 into the data dir; do this first
install(port=P, source={"file": F})      # asks; refuses a wrong-SoftDevice image
nrf52_factory_erase(port=P)                # wipes config/node DB/keys, keeps firmware
```
Keep passing the board's app port (its by-id path): the drive is matched by USB serial even
after that link disappears in UF2 mode. The drive must be mounted; a headless host that does not
automount needs it mounted by hand. The board stays in UF2 mode until a UF2 lands or it is
power-cycled.

**nRF52 by serial DFU**: `install(port=P, source={"file": ZIP})` touches the board into DFU
itself and fails unless adafruit-nrfutil printed `Device programmed.`. A board left sitting in
serial DFU takes the package with no touch:
`vendor(tool="nrfutil", args=["dfu", "serial", "--package", ZIP, "--port", "/dev/ttyACM0", "-b", "115200", "--singlebank"], confirm=True)`.

`touch_1200bps(port=P)` bounces nRF52, ESP32-S3 and RP2040 boards into their bootloader and
reports the bootloader's port. Chip-specific escape hatch when `install` can't help:
`vendor(tool="esptool" | "nrfutil" | "picotool", args=[...])` (read-only subcommands run at
once; anything else asks). For a
wedged USB device, power-cycle the hub port with `uhubctl_list` / `uhubctl_power` /
`uhubctl_cycle`.
> **Linux:** `uhubctl` requires udev rules to work without root. Run
> `meshtastic-mcp doctor` — it will detect the permission issue and print
> the exact `sudo curl … && sudo udevadm trigger` command to fix it.

### BLE OTA DFU (manual — no MCP tool covers this yet)

Every `install` path above is USB (serial or UF2). There is
no MCP tool for the *wireless* leg — the Nordic legacy BLE DFU that the Meshtastic
Android app uses for its in-app bootloader/firmware upgrade, and that the
Meshtastic nRF52 Bootloader (`meshtastic/nRF52_Bootloader`)
speaks natively. To validate that path from a dev machine instead of a phone:

1. **Buttonless jump from app mode is one of two GATT services, chosen at compile
   time — check which before assuming a UUID.** `NRF52Bluetooth.cpp` picks
   `BLEDfuSecure` (Nordic Secure DFU, service `0xFE59`, control characteristic
   `8ec90003-f315-4f60-9fb8-838830daea50`) only when the board's `variant.h`
   defines `BLE_DFU_SECURE` — today that's `wio-t1000-s` alone. Every other
   nRF52 board, **including RAK4631**, falls through to plain `BLEDfu`
   (Adafruit's Bluefruit library): legacy service `00001530-...`, control
   characteristic `00001531-...`. Either way: enable notifications/indications
   on the control characteristic, write `0x01` to it, and the node disconnects
   and reboots into the bootloader, advertising under a new BLE name (Meshtastic
   nRF52 Bootloader boards use `<BOARD>_DFU`, e.g. `4631_DFU` for RAK4631 — see that repo's
   README "BLE advertising names" table).
2. **Legacy DFU transfer in bootloader mode.** The bootloader's own GATT
   service is always the older Nordic Legacy DFU (`00001530-...`, control
   point `...1531`, data `...1532`) regardless of which service the app used
   to jump there. [`recrof/nrf_dfu_py`](https://github.com/recrof/nrf_dfu_py)
   (pure Python + `bleak`) speaks **only** this legacy service (its
   `DFU_SERVICE_UUID` constant is the `00001530-...` one, full stop) — it has
   no path for a `BLE_DFU_SECURE` board like `wio-t1000-s`, so it's a match
   for RAK4631 and most other nRF52 targets, not a universal tool. Clone it,
   `pip install bleak`, then from that checkout:
   `python3 dfu_cli.py --scan <firmware-or-bootloader.zip> <device-name-or-addr>`.
   Use the `*-ota.zip` release asset (not the `.uf2`/`.hex`) — that's the format
   this DFU protocol expects. One call does both legs unassisted — its
   `jump_to_bootloader()` sends the exact same 2-byte legacy opcode write
   described in step 1, then it rescans and transfers — so giving it the
   **app-mode** name/address up front is usually enough; you don't need a
   separate manual jump. Do the jump as its own step only when you need to
   debug the jump in isolation (its post-jump bootloader rescan matches by
   substring against a literal `"DFU"`/MAC-increment heuristic, not the
   board's exact advertised name, so once already in bootloader mode,
   re-running against the bootloader's own `<BOARD>_DFU` name is the more
   reliable retry).
3. **Finding the device's BLE name is a scan-and-match, and a name prefix can be
   ambiguous — resolve it to exactly one device before acting.** The app's
   advertised name is `<short_name>_<hex><hex>` where the hex suffix is the
   last two bytes of the nRF52's FICR `DEVICEADDR` (`getDeviceName()` in
   `firmware/src/main.cpp`) — **not** derived from `my_node_num`/`inspect`'s
   node id in any way you can compute offline. Scan (`BleakScanner.discover`)
   and match by the known `short_name` prefix, but confirm the scan turned up
   exactly one match before connecting: `nrf_dfu_py` (and most such tools)
   connects to the first match among the names/addresses you give it, so an
   ambiguous prefix on a mesh with more than one device sharing it can jump or
   flash the wrong node.
4. **`bluetooth.mode = RANDOM_PIN` needs a human (or the app) watching for the
   passkey — an unattended agent session can't complete pairing, screen or no
   screen.** The firmware sends the 6-digit passkey to `BluetoothStatus` (the
   app's pairing UI reads it from there) and additionally shows it on-screen
   `#if HAS_SCREEN` — some RAK4631 builds do have one (e.g. `rak_wismesh_pocket`
   is explicitly "rak4631 pin map + OLED" in `platformio.ini`; don't assume
   `hw_model: RAK4631` alone tells you whether the running build has a
   display). `capture_screen` (see *Hardware UI checks* above) can read an
   OLED when one's present — worth reaching for on a future attempt, since
   neither it nor a live debug log was actually checked in real time here;
   the passkey/display behavior below is reasoned from firmware source
   after the fact, not confirmed against what this device actually showed.
   Whether the passkey is visible in a live debug log (`security.debug_log_api_enabled`)
   depends on the exact firmware build — `onPairingPasskey`'s `LOG_INFO`
   included the passkey digits in
   `v2.7.26.54e0d8d` (what this was tested against) and still does on
   `develop`, but a firmware security fix logged only `match_request` for a
   stretch of the 2.7.x line in between (redacting pairing secrets from
   logs) — don't assume the log line carries it on an arbitrary build; the
   app's pairing UI is the one path guaranteed to receive it regardless.
   Neither was being watched in a scripted `bleak` session here, so the OS
   pairing prompt sat with nothing to type in and the connection was
   dropped. This is
   expected `RANDOM_PIN` behavior, not a bug — switch to `FIXED_PIN` (a value
   you already know) for scripted/headless testing instead of chasing it.
   `configure` snapshots `bluetooth.mode`/`fixed_pin` before changing them;
   restore them with `configure(restore=<snapshot>)` once testing is done — a device left in `FIXED_PIN` carries a
   known, reusable pairing credential indefinitely otherwise.
   Separately, firmware `develop` (2.8) does carry two real nRF52 BLE-pairing
   fixes not yet in 2.7.x that are worth knowing about if pairing looks
   flaky on a 2.7.x build: a passkey callback that wasn't restored after a
   BT disable/re-enable cycle without a reboot (#11027), and a BLE-task
   stack overflow that could crash the device mid-pairing on nrf52840
   targets (#11190).

**macOS-specific friction**, all one-time per machine/device pair, not per session:
- Bluetooth must be explicitly on (Control Center) — `bleak`/CoreBluetooth error
  clearly (`BleakBluetoothNotAvailableError: POWERED_OFF`) when it isn't, so this
  fails fast rather than silently.
- The terminal app driving the script needs Bluetooth permission granted
  (System Settings → Privacy & Security → Bluetooth) — without it, scanning
  either errors or (confusingly) just finds nothing.
- A newly-enumerated USB device can trigger a silent macOS "accessory" permission
  popup that hides the port from `devices`/`ls /dev/cu.*` until approved —
  if a device that was just flashed or reset seems to vanish from USB entirely,
  check for that popup before assuming a bad flash.

## Hardware UI checks

`send_input_event` drives the device's buttons; `capture_screen` photographs the panel (camera/OCR
optional — see `doctor` for the `[ui]` extra). This is device-only; for app UI use `meshtastic-e2e`.

**Prefer `capture_display` when the firmware supports it.** It reads the framebuffer over the
phone API instead of photographing the panel, so it needs no camera and the pixels are exact
(and OCR is far more reliable). It covers mono OLEDs, color TFTs and MUI (LVGL) panels. It times out on firmware without screen-mirror support — fall
back to `capture_screen` then. On an MUI (color LVGL) device it also times out while another
client, such as the Android app, is mirroring: the firmware serves MUI rects to one connection
at a time. A `blank: true` result means the panel had hit
`display.screen_on_secs`, not that the capture failed: capture again with `wake="BACK"` (or any
event harmless on that screen), which presses it on the capture's own connection just before
arming. A separate `send_input_event` first does not help on a device whose handshake outlasts
its screen timeout, and a slept MUI panel captures as solid black.

### Screenshots for documentation

`capture_display` is the right tool for docs shots — exact pixels, no camera rig. Two
arguments exist for this case:

- **`out_path`** names the file (parents are created). Without it every call overwrites one
  scratch path, so a ten-screen walkthrough ends up as a single image. It must be a `.png`
  inside `MESHTASTIC_MCP_CAPTURE_DIR` (default: the server's working directory), and
  relative paths resolve there; point the variable at the docs checkout for a docs run.
- **`scale`** enlarges by an integer factor, nearest-neighbour, max 8. A 128x64 OLED is
  unreadable at native size on a page; 2x-4x is usually right. The pixel grid stays hard on
  purpose — a smoothed device UI reads as a blurry photo of a screen. `width`/`height` in the
  result describe the written image, `panel_width`/`panel_height` the device.

A walk through several screens is one `drive_display_start` job: it keeps a single connection
with mirroring armed, presses each step and writes a PNG after it, so the panel never sleeps
between shots and the handshake is paid once:

```
drive_display_start(port=P, out_dir="docs/img", scale=2, wake="BACK", steps=[
    {"event": "DOWN", "label": "nodes"},
    {"event": "USER_PRESS", "touch_x": 20, "touch_y": 140, "label": "chats"},
])
job(job_id, wait_s=50)              # until status=done; steps[] holds each PNG and changed_px
```

`changed_px` is the diff against the previous shot; 0 means the press changed nothing on screen.
`capture_display` alone takes one shot per connection (settle + handshake + quiet period, a
minute on a T-Deck with a large node DB). Check `blank` on every shot.

### Driving the UI remotely

`send_input_event` and `drive_display_start` speak the same events, and they are real presses:

- **D-pad and select:** `UP`/`DOWN`/`LEFT`/`RIGHT`, `SELECT`, `SELECT_LONG`. On MUI the arrows
  are D-pad keys whose effect depends on the focused widget (in the sidebar, `UP`/`DOWN` change
  screens and `LEFT`/`RIGHT` do nothing).
- **Touch (MUI):** a tap is `USER_PRESS` with `touch_x`/`touch_y`; `SELECT` with coordinates is
  a touch long-press (on a chat row it reveals `DEL`).
- **Typing:** any code with `kb_char` set, e.g. `{"event": 255, "kb_char": 104}` types `h`.
- **Going back:** `BACK` is the back action. On BaseUI it closes an open menu and otherwise goes to
  the previous frame (firmware without meshtastic/firmware#12358 closes menus only on `CANCEL`).
  A touch MUI board (`inspect(detail="full")` → `summary.display.has_touch`) has no back key by design and ignores
  `BACK`: tap the sidebar instead. Avoid `CANCEL` as a back key: on BaseUI with no menu open it
  turns the screen off.
- **Enter sends:** `SELECT` or `SELECT_LONG` with a focused MUI text field submits it as a mesh
  message. Clear typed text (or leave the chat) before any Enter-like step.

Two things that bite in practice:

- **The content varies shot to shot.** Node counts, battery percentage and the clock all move,
  so a set captured over several minutes looks inconsistent. `push_fake_nodedb` (and
  `sim(action="start")` for a simulated mesh) give every screenshot the same mesh; `configure(changes={"owner.long_name": ...})` fixes
  the device name.
- **Real screens leak real data.** Node long_names, positions and message text from your own
  mesh end up in the image. Seed a fake node DB before capturing anything destined for a public
  page, rather than editing PNGs afterwards.

## Grounded answers

- **`android_docs_search` / `android_docs_fetch`** — Android/Compose/API questions answered from
  the official Knowledge Base (no guessing) when working with the Android app.

## Prompts (slash workflows)

- **`bringup_device`** — connect + verify a healthy baseline (firmware, region, node count).
- **`inbound_loop`** / **`triage_e2e_failure`** — e2e workflows (see the `meshtastic-e2e` skill).

## Hard rules

1. One call per serial port at a time (exclusive non-blocking lock).
2. A config write lives in RAM until a reboot commits it; re-read after the reboot.
3. Destructive tools ask the user when the client can; never pass `confirm=True` for an action
   the user has not agreed to.
4. `admin(action="factory_reset_full")` wipes BLE bonds + the identity key; `action="factory_reset"` keeps them.
5. Prefer the recorder windows over ad-hoc reads: they're timestamped and align with app snapshots.
6. A canceled flash keeps writing; follow its `job_id` with `job` before touching the board.
7. Never apply a ground key whose `verified` is false, and never apply one without telling the
   operator the old node id first — the change is not reversible without the previous key.
