---
name: meshtastic-device-ops
license: GPL-3.0-only
description: Discover, connect to, drive, configure, observe, and flash Meshtastic devices through the Meshtastic MCP server. Use when an agent needs to bring up a radio (serial or TCP), read or change device/channel config, send messages, inspect the node DB, watch live packets/telemetry/logs via the recorder, recover a bricked device, or build/flash firmware. Covers the non-e2e MCP tool surface; for cross-plane device↔app testing use the `meshtastic-e2e` skill.
---

# Meshtastic device operations (via the MCP server)

The Meshtastic MCP server exposes ~43 tools plus resources and prompts. This skill maps
common operator intents to the right tools and the safe order to use them.

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
