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

## Connect

Two transports, same admin surface:
- **Serial:** a USB port from `list_devices` (e.g. `/dev/cu.usbmodem101`).
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
for a follow-on `build_start`:
```
list_devices                     # find candidates (include_unknown=true to see every port)
device_info <port>               # firmware version, node identity, channel summary
list_nodes <port>                # the node DB (long/short name, SNR, last-heard, position)
```

One MCP call per serial port at a time — the port lock is **exclusive and non-blocking**:
contention fails fast with "busy … Retry shortly" (it does not queue). Open → act → close.

## Configure (mutating — confirm-gated)

```
get_config <port> <section>      # lora | device | position | power | network | display | ...
set_config <port> <section> <field>=<value> ...
get_channel_url <port>           # the shareable channel URL (keys)
set_channel_url <port> <url>
set_owner <port> --long ... --short ...
```
After a write, **reboot then re-read** to prove it persisted to NVS, not just RAM
(`reboot <port>` → `get_config`). Region (`lora.region`) and `network.enabled_protocols`
are the two that bite — see `meshtastic-e2e` `topology.md`.

## Message + observe

```
send_text <port> <text> [--dest <nodeId>]   # broadcast (^all) or directed
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

## Recover + flash (firmware capability)

```bash
# Board lookup — use the CLI (no schema overhead):
meshtastic-mcp boards --query <slug>                     # find the env name
meshtastic-mcp boards get <env>                          # confirm arch + upload_speed
```
```
# Build + flash via MCP (async to avoid 60 s client timeout):
build_start <env>                                        # returns build_id immediately
build_poll <build_id>                                    # poll until status=done
pio_flash <env> <port> / erase_and_flash <env> <port> / update_flash <env> <port>
touch_1200bps <port>          # bounce into the bootloader (nRF/RP2040)
```
Chip-specific escape hatches when pio can't help: `esptool_*`, `nrfutil_*`, `picotool_*`
(raw passthroughs; destructive ones are confirm-gated). For a wedged USB device, power-cycle
the hub port with `uhubctl_list` / `uhubctl_power` / `uhubctl_cycle`.
> **Linux:** `uhubctl` requires udev rules to work without root. Run
> `meshtastic-mcp doctor` — it will detect the permission issue and print
> the exact `sudo curl … && sudo udevadm trigger` command to fix it.

### BLE OTA DFU (manual — no MCP tool covers this yet)

Every `flash`/`pio_flash`/`nrfutil_dfu` path above is USB (serial or UF2). There is
no MCP tool for the *wireless* leg — the Nordic legacy BLE DFU that the Meshtastic
Android app uses for its in-app bootloader/firmware upgrade, and that an
Adafruit/OTAFIX-family nRF52 bootloader (e.g. `meshtastic/Adafruit_nRF52_Bootloader_OTAFIX`)
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
   and reboots into the bootloader, advertising under a new BLE name (OTAFIX
   boards use `<BOARD>_DFU`, e.g. `4631_DFU` for RAK4631 — see that repo's
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
   `firmware/src/main.cpp`) — **not** derived from `my_node_num`/`device_info`'s
   node id in any way you can compute offline. Scan (`BleakScanner.discover`)
   and match by the known `short_name` prefix, but confirm the scan turned up
   exactly one match before connecting: `nrf_dfu_py` (and most such tools)
   connects to the first match among the names/addresses you give it, so an
   ambiguous prefix on a mesh with more than one device sharing it can jump or
   flash the wrong node.
4. **`bluetooth.mode = RANDOM_PIN` needs a human or the app watching for the
   passkey — a scripted client alone can't complete pairing.** The firmware
   sends the 6-digit passkey to `BluetoothStatus` (the app's pairing UI reads
   it from there) and shows it on-screen only `#if HAS_SCREEN` (most RAK4631
   builds, e.g. the WisMesh Pocket, have none). Whether it's *also* visible in
   a live debug log (`set_debug_log_api`) depends on the exact firmware
   build — `onPairingPasskey`'s `LOG_INFO` included the passkey digits in
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
   `get_config`-read the current `bluetooth.mode`/`fixed_pin` *before* changing
   anything, and restore them (`set_config` + `reboot` + `get_config` to
   confirm) once testing is done — a device left in `FIXED_PIN` carries a
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
  popup that hides the port from `list_devices`/`ls /dev/cu.*` until approved —
  if a device that was just flashed or reset seems to vanish from USB entirely,
  check for that popup before assuming a bad flash.

## Hardware UI (OLED) checks

`send_input_event` drives the device's buttons; `capture_screen` grabs the OLED (camera/OCR
optional — see `doctor` for the `[ui]` extra). This is device-only; for app UI use `meshtastic-e2e`.

## Grounded answers

- **`android_docs_search` / `android_docs_fetch`** — Android/Compose/API questions answered from
  the official Knowledge Base (no guessing) when working with the Android app.

## Prompts (slash workflows)

- **`bringup_device`** — connect + verify a healthy baseline (firmware, region, node count).
- **`inbound_loop`** / **`triage_e2e_failure`** — e2e workflows (see the `meshtastic-e2e` skill).

## Hard rules

1. One call per serial port at a time (exclusive non-blocking lock).
2. Mutations are confirm-gated and reversible-by-reboot only for RAM writes — re-read after reboot.
3. `factory_reset(full=true)` wipes BLE bonds + the identity key; `full=false` keeps them.
4. Prefer the recorder windows over ad-hoc reads — they're timestamped and align with app snapshots.
