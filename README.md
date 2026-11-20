# meshtastic-mcp

<!-- mcp-name: io.github.meshtastic/meshtastic-mcp -->

[![CI](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A home for AI tooling to **discover, drive, observe, and test** Meshtastic devices and the
apps that talk to them. An [MCP](https://modelcontextprotocol.io) server plus a bundled
agent skill set for closed-loop device ↔ app end-to-end testing.

> Extracted and decoupled from the Meshtastic firmware repo's `mcp-server/` so it can be
> installed and distributed on its own. The firmware-build tools become an optional
> capability that lights up only when a firmware checkout is present.

## Quick start (60 seconds)

Plug in a Meshtastic device, then:

```bash
uvx meshtastic-mcp doctor          # check what's available
```

Inside Claude Code (or any MCP client registered with `meshtastic`):

```
devices()                               → the attached radios, by name
inspect(node="heltec-v3-aa13")          → firmware, region, node count
inspect(node="heltec-v3-aa13", what=["lora", "nodes"])  → LoRa config and mesh peers
configure(node="heltec-v3-aa13", changes={"security.debug_log_api_enabled": True})
send(node="heltec-v3-aa13", text="hello mesh", wait_for_tx=True)  → tx_confirmed
```

A node never sees its own transmission on the receive path, so `packets_window` cannot confirm a
local send; `wait_for_tx=True` reads the firmware's TX log line instead.

No firmware checkout required. Core tools work against any serial or TCP device.

## Install

```bash
# Install the CLI + MCP server (uv recommended; also works with pipx)
uv tool install 'meshtastic-mcp[ui]'       # includes camera/OCR support
# or without UI extras:
uv tool install meshtastic-mcp
# other extras: [tasks] MCP background tasks · [web] FleetSuite bench UI · [sdr] RF-compliance oracle · [test] pytest harness

# Unreleased master, straight from git (no build step):
uv tool install 'meshtastic-mcp[ui] @ git+https://github.com/meshtastic/meshtastic-mcp'

# Install from a local checkout (dev):
uv tool install --editable '/path/to/meshtastic-mcp[ui]'

# One step: register the server in your MCP client + install the bundled skills
meshtastic-mcp install                 # claude-code user scope; --client cursor|windsurf|claude-desktop
meshtastic-mcp install --local         # register the current interpreter (editable/dev install)
meshtastic-mcp install --print         # just emit the JSON snippet (edit nothing)
meshtastic-mcp uninstall               # remove the registration  (--purge-skills to drop skills too)

# Or do the pieces manually:
claude mcp add meshtastic -s user -- meshtastic-mcp     # register with Claude Code
meshtastic-mcp skills install           # → ~/.agents/skills/ (device-ops, e2e, org-knowledge)
meshtastic-mcp skills uninstall         # remove them
```

`install` edits the client's `mcpServers` JSON in place (preserving other entries) — it picks the
right config path per `--client` (claude-code / claude-desktop / cursor / windsurf) and `--scope`
(user / project), or pass `--config PATH`. Add capability env vars with `--env KEY=VALUE`.
Restart the MCP client to pick up the change.

### Updating

```bash
uv tool upgrade meshtastic-mcp        # PyPI install: latest release
uv tool install 'meshtastic-mcp[ui] @ git+https://github.com/meshtastic/meshtastic-mcp'   # git install: re-run to take master's head
git -C /path/to/meshtastic-mcp pull   # editable install: re-run its install when dependencies change
```

The MCP client keeps running the old server until you restart it.

### Source repos (optional capabilities)

Set env vars pointing at local checkouts to activate firmware-build, Android, and Apple
capabilities. The `provision` subcommand clones everything automatically:

```bash
meshtastic-mcp provision              # clones firmware + android + apple, prints export commands
source ~/.local/share/meshtastic-mcp/repos/.env

# Then add to your shell profile:
export MESHTASTIC_FIRMWARE_ROOT=~/meshtastic/firmware
export MESHTASTIC_ANDROID_ROOT=~/meshtastic/android
export MESHTASTIC_APPLE_ROOT=~/meshtastic/apple

# Add to your MCP client config so the server process picks them up:
# { "env": { "MESHTASTIC_FIRMWARE_ROOT": "/path/to/firmware", ... } }
```

> **esptool** — if PlatformIO is installed, esptool is auto-discovered from the
> PlatformIO penv and wrapped automatically. No manual setup required.

```bash
meshtastic-mcp doctor          # verify what's active after setup
```

## Capabilities

A **portable core** always works against any connected device (serial or `tcp://host:port`)
with no firmware checkout. Optional capabilities activate when their prerequisite is present:

| Capability | Prerequisite | Adds |
|---|---|---|
| **core** | none | discovery, serial+TCP transport, `inspect`/`configure`/`send`/reboot, recorder + log/telemetry/packet/event queries, **replay** (simulated TCP device + synthetic mesh + fuzzer), input-events, uhubctl, `esptool`/`nrfutil`/`picotool`, the nRF52 bootloader's UF2 drive (`nrf52_*`) |
| **firmware** | `MESHTASTIC_FIRMWARE_ROOT` + PlatformIO `pio` | build / clean / flash / OTA / board enum / userPrefs |
| **android** | `android` CLI + `adb` | Android-emulator + native-node orchestration for hardware-free e2e |
| **apple** | `xcrun` (+ `idb` for UI) | iOS Simulator / macOS-app orchestration for hardware-free e2e (`ios_*`); `screenshot_set_*` registers with this or android and captures matching Android and iOS screens, see [docs/screenshot-set.md](docs/screenshot-set.md) |
| **local-model** | a reachable Ollama or OpenAI-compatible `llama-server` (or a `llama` binary to start one) | offload tools that push token-heavy work onto a local GPU: summarize/triage recorder windows, e2e-failure first pass, and an offline **vision oracle**; see [docs/local-models.md](docs/local-models.md) |
| **sdr** | `[sdr]` extra (bundles `pyrtlsdrlib`, a prebuilt librtlsdr) + an RTL-SDR dongle | RF-compliance oracle: `rf_scan` occupancy checks and `rf_confirm_tx` on-air verification, no second radio needed; see [docs/rf-oracle.md](docs/rf-oracle.md). *macOS/Homebrew note:* a system `librtlsdr` from Homebrew is the osmocom fork and lacks `rtlsdr_set_dithering`, so `import rtlsdr` fails; the bundled `pyrtlsdrlib` avoids this and is preferred by pyrtlsdr's loader. |
| **tak** | the meshtastic-tak SDK, which is not on PyPI: install it with the command `doctor` prints, or `uv sync --group tak` in a checkout | real TAKPacketV2 zstd wire compression for the replay sim's TAK squad (`profile tak.wire="v2"`) and its CoT stream to ATAK |
| **sdk-cli** *(experimental)* | Kotlin SDK headless CLI | alternate device-IO backend over the JVM CLI; see [docs/sdk-cli-bridge.md](docs/sdk-cli-bridge.md) |
| **discord** | a read-only bot token (`$DISCORD_BOT_TOKEN` or `<user-config-dir>/meshtastic-mcp/discord.token`; `doctor` prints the path) | read the Meshtastic Discord server: server-side search, history, threads/forum posts, pins, mentions of you, with a per-message role-derived `trust` tier (`discord_*`); stdlib only, never posts; see [docs/discord.md](docs/discord.md) |
| **ble-sniffer** | an [nRF Sniffer for Bluetooth LE](https://www.nordicsemi.com/Products/Development-tools/nRF-Sniffer-for-Bluetooth-LE) dongle (Nordic VID `0x1915`/PID `0x522A`) + Nordic's `nrfutil-ble-sniffer` plugin | capture the **phone-to-node BLE link** from outside both ends (`ble_sniff_start`/`_poll`/`_stop`), the independent oracle for when the app says "no devices found" and the node says "advertising". `ble_sniff_status` is core. Receive-only; output is untrusted (a BLE name is arbitrary text from any radio in range); see [docs/ble-sniffer.md](docs/ble-sniffer.md) |
| **mvgrind** | the [`mvgrind`](https://github.com/miketweaver/mvgrind) binary + an OpenCL driver | grind a **vanity NodeNum or app color** on the GPU (`vanity_grind_start`/`_poll`/`_stop`): a node's number is `crc32(x25519_public_key)` and the apps paint it with the low 24 bits, so a chosen id or color means searching the keyspace. `vanity_preview` and `vanity_apply` are core, so a key ground elsewhere still applies here; see [docs/vanity.md](docs/vanity.md) |

The active set is logged at startup (`meshtastic-mcp capabilities active: …`).

Beyond tools, the server exposes MCP **resources** (`meshtastic://doctor`,
`meshtastic://capabilities`, and the templated `meshtastic://e2e/{loop}` for bundled e2e recipes)
and **prompts** (`triage_e2e_failure`, `bringup_device`, `inbound_loop`). The `android_docs_search`/
`android_docs_fetch` tools answer Android/Compose questions grounded in the official Knowledge Base.

Tools with a result type in `schemas.py` advertise it as an output schema. A device, flash or
hardware tool that fails returns a tool error (`isError`) carrying the cause and the tail of any
command output.

### Source repos — two modes

Build tools (`build_meshtasticd.sh`, `build_android_apk.sh`, `build_apple.sh`) work in two modes:

**Dev with existing checkouts** — point env vars at your local trees:

```bash
export MESHTASTIC_FIRMWARE_ROOT=~/firmware
export MESHTASTIC_ANDROID_ROOT=~/Meshtastic-Android
export MESHTASTIC_APPLE_ROOT=~/Meshtastic-Apple
```

**Portable / fresh environment** — clone everything in one shot:

```bash
meshtastic-mcp provision              # clones all three repos, writes a .env file
source ~/.local/share/meshtastic-mcp/repos/.env
```

`provision` respects any env vars already set (skips those repos) and writes a sourceable `.env`
for the ones it clones. Run `doctor` afterwards to confirm all binary deps are present too.

### `doctor` — probe the environment

Run before starting e2e work (also exposed as the `doctor` MCP tool, so an agent can
self-provision):

```bash
meshtastic-mcp doctor          # per-dependency status + exact, platform-aware install commands
meshtastic-mcp doctor --json   # machine-readable; `fix_commands[]` is ready to run
```

Probes binaries (`pio`, `adb`, `xcrun`, `idb_companion`, `fb-idb`, `ffmpeg`, `uhubctl`, OCR)
**and** source repo roots (`MESHTASTIC_FIRMWARE_ROOT`, `MESHTASTIC_ANDROID_ROOT`,
`MESHTASTIC_APPLE_ROOT`), reporting `ok`/`missing`/`degraded` with the exact command to fix each
— including non-obvious ones (e.g. `idb_companion` lives in the `facebook/fb` tap, `fb-idb`
requires Python ≤ 3.12).

### Build scripts

| Script | Builds | Key flags |
|---|---|---|
| `scripts/build_meshtasticd.sh` | Native `meshtasticd` daemon | `--env native\|native-macos`, `--ref`, `--firmware-dir` / `MESHTASTIC_FIRMWARE_ROOT` |
| `scripts/build_android_apk.sh` | Meshtastic-Android APK | `--ref`, `--variant`, `--source-dir` / `MESHTASTIC_ANDROID_ROOT` |
| `scripts/build_apple.sh` | Meshtastic iOS-Simulator `.app` | `--ref`, `--sim`, `--source-dir` / `MESHTASTIC_APPLE_ROOT` |

All scripts clone fresh if no source dir is provided, accept `--ref` to pin a git sha/tag/branch,
and print a machine-readable `<component>-sha=<sha>` line for CI provenance.


## Tools

Each tool's parameters and description are in [docs/tools.md](docs/tools.md), generated from the
server's registered tools. The toolsets, as `MESHTASTIC_MCP_TOOLSETS` names them:

<!-- BEGIN GENERATED TOOLSETS: `meshtastic-mcp docs generate` -->
| Toolset | Tools |
| --- | --- |
| `core` | [`capture_display`](docs/tools.md#capture_display), [`claim`](docs/tools.md#claim), [`config_diff`](docs/tools.md#config_diff), [`config_snapshot`](docs/tools.md#config_snapshot), [`config_snapshots_list`](docs/tools.md#config_snapshots_list), [`configure`](docs/tools.md#configure), [`devices`](docs/tools.md#devices), [`doctor`](docs/tools.md#doctor), [`drive_display_poll`](docs/tools.md#drive_display_poll), [`drive_display_start`](docs/tools.md#drive_display_start), [`events_window`](docs/tools.md#events_window), [`factory_reset`](docs/tools.md#factory_reset), [`inject_frame`](docs/tools.md#inject_frame), [`inspect`](docs/tools.md#inspect), [`job`](docs/tools.md#job), [`list_devices`](docs/tools.md#list_devices), [`logs_window`](docs/tools.md#logs_window), [`mark_event`](docs/tools.md#mark_event), [`packets_window`](docs/tools.md#packets_window), [`query`](docs/tools.md#query), [`reboot`](docs/tools.md#reboot), [`recorder_export`](docs/tools.md#recorder_export), [`recorder_pause`](docs/tools.md#recorder_pause), [`recorder_resume`](docs/tools.md#recorder_resume), [`recorder_status`](docs/tools.md#recorder_status), [`release`](docs/tools.md#release), [`send`](docs/tools.md#send), [`send_input_event`](docs/tools.md#send_input_event), [`set_channel_url`](docs/tools.md#set_channel_url), [`shutdown`](docs/tools.md#shutdown), [`telemetry_timeline`](docs/tools.md#telemetry_timeline), [`triage_bundle`](docs/tools.md#triage_bundle), [`wait_for`](docs/tools.md#wait_for) |
| `android` | [`android_clear_logcat`](docs/tools.md#android_clear_logcat), [`android_docs_fetch`](docs/tools.md#android_docs_fetch), [`android_docs_search`](docs/tools.md#android_docs_search), [`android_find_text`](docs/tools.md#android_find_text), [`android_poll_for_text`](docs/tools.md#android_poll_for_text), [`android_read_logcat`](docs/tools.md#android_read_logcat), [`android_render_compose_preview`](docs/tools.md#android_render_compose_preview), [`android_resolve`](docs/tools.md#android_resolve), [`android_screenshot`](docs/tools.md#android_screenshot), [`android_swipe`](docs/tools.md#android_swipe), [`android_tap`](docs/tools.md#android_tap), [`android_type_text`](docs/tools.md#android_type_text), [`android_ui_dump`](docs/tools.md#android_ui_dump), [`android_version_lookup`](docs/tools.md#android_version_lookup), [`atak_drive_route`](docs/tools.md#atak_drive_route), [`atak_drive_stop`](docs/tools.md#atak_drive_stop), [`atak_fleet_down`](docs/tools.md#atak_fleet_down), [`atak_fleet_status`](docs/tools.md#atak_fleet_status), [`atak_fleet_up`](docs/tools.md#atak_fleet_up), [`atak_share_item`](docs/tools.md#atak_share_item) |
| `apple` | [`ios_boot`](docs/tools.md#ios_boot), [`ios_list_simulators`](docs/tools.md#ios_list_simulators), [`ios_open_url`](docs/tools.md#ios_open_url), [`ios_screenshot`](docs/tools.md#ios_screenshot), [`ios_tap`](docs/tools.md#ios_tap), [`ios_type_text`](docs/tools.md#ios_type_text), [`ios_ui_dump`](docs/tools.md#ios_ui_dump), [`screenshot_set_poll`](docs/tools.md#screenshot_set_poll), [`screenshot_set_start`](docs/tools.md#screenshot_set_start) |
| `discord` | [`discord_channels`](docs/tools.md#discord_channels), [`discord_context`](docs/tools.md#discord_context), [`discord_forum_posts`](docs/tools.md#discord_forum_posts), [`discord_member`](docs/tools.md#discord_member), [`discord_mentions`](docs/tools.md#discord_mentions), [`discord_pins`](docs/tools.md#discord_pins), [`discord_read`](docs/tools.md#discord_read), [`discord_search`](docs/tools.md#discord_search), [`discord_status`](docs/tools.md#discord_status), [`discord_thread`](docs/tools.md#discord_thread) |
| `firmware` | [`build`](docs/tools.md#build), [`build_poll`](docs/tools.md#build_poll), [`build_start`](docs/tools.md#build_start), [`clean`](docs/tools.md#clean), [`erase_and_flash`](docs/tools.md#erase_and_flash), [`flash_poll`](docs/tools.md#flash_poll), [`flash_start`](docs/tools.md#flash_start), [`get_board`](docs/tools.md#get_board), [`list_boards`](docs/tools.md#list_boards), [`pio_flash`](docs/tools.md#pio_flash), [`push_fake_nodedb`](docs/tools.md#push_fake_nodedb), [`update_flash`](docs/tools.md#update_flash), [`userprefs_get`](docs/tools.md#userprefs_get), [`userprefs_manifest`](docs/tools.md#userprefs_manifest), [`userprefs_reset`](docs/tools.md#userprefs_reset), [`userprefs_set`](docs/tools.md#userprefs_set), [`userprefs_testing_profile`](docs/tools.md#userprefs_testing_profile) |
| `hardware` | [`capture_screen`](docs/tools.md#capture_screen), [`esptool_chip_info`](docs/tools.md#esptool_chip_info), [`esptool_erase_flash`](docs/tools.md#esptool_erase_flash), [`esptool_raw`](docs/tools.md#esptool_raw), [`nrf52_backup_firmware`](docs/tools.md#nrf52_backup_firmware), [`nrf52_bootloader_info`](docs/tools.md#nrf52_bootloader_info), [`nrf52_enter_uf2`](docs/tools.md#nrf52_enter_uf2), [`nrf52_factory_erase`](docs/tools.md#nrf52_factory_erase), [`nrf52_flash_uf2`](docs/tools.md#nrf52_flash_uf2), [`nrfutil_dfu`](docs/tools.md#nrfutil_dfu), [`nrfutil_raw`](docs/tools.md#nrfutil_raw), [`picotool_info`](docs/tools.md#picotool_info), [`picotool_load`](docs/tools.md#picotool_load), [`picotool_raw`](docs/tools.md#picotool_raw), [`serial_close`](docs/tools.md#serial_close), [`serial_list`](docs/tools.md#serial_list), [`serial_open`](docs/tools.md#serial_open), [`serial_read`](docs/tools.md#serial_read), [`touch_1200bps`](docs/tools.md#touch_1200bps), [`uhubctl_cycle`](docs/tools.md#uhubctl_cycle), [`uhubctl_list`](docs/tools.md#uhubctl_list), [`uhubctl_power`](docs/tools.md#uhubctl_power) |
| `local-model` | [`local_model_serve`](docs/tools.md#local_model_serve), [`local_model_serve_stop`](docs/tools.md#local_model_serve_stop), [`local_model_status`](docs/tools.md#local_model_status), [`summarize_window`](docs/tools.md#summarize_window), [`triage_window`](docs/tools.md#triage_window), [`vision_oracle`](docs/tools.md#vision_oracle) |
| `replay` | [`cot_relay_start`](docs/tools.md#cot_relay_start), [`cot_relay_status`](docs/tools.md#cot_relay_status), [`cot_relay_stop`](docs/tools.md#cot_relay_stop), [`replay_fuzz_presets`](docs/tools.md#replay_fuzz_presets), [`replay_inject`](docs/tools.md#replay_inject), [`replay_inject_beacon`](docs/tools.md#replay_inject_beacon), [`replay_inject_client_notification`](docs/tools.md#replay_inject_client_notification), [`replay_inject_fileinfo`](docs/tools.md#replay_inject_fileinfo), [`replay_inject_traceroute`](docs/tools.md#replay_inject_traceroute), [`replay_inject_waypoint`](docs/tools.md#replay_inject_waypoint), [`replay_start`](docs/tools.md#replay_start), [`replay_status`](docs/tools.md#replay_status), [`replay_stop`](docs/tools.md#replay_stop) |
| `rf` | [`ble_sniff_poll`](docs/tools.md#ble_sniff_poll), [`ble_sniff_start`](docs/tools.md#ble_sniff_start), [`ble_sniff_status`](docs/tools.md#ble_sniff_status), [`ble_sniff_stop`](docs/tools.md#ble_sniff_stop), [`pa_measure`](docs/tools.md#pa_measure), [`pa_meter_status`](docs/tools.md#pa_meter_status), [`pa_sweep`](docs/tools.md#pa_sweep), [`rf_confirm_tx`](docs/tools.md#rf_confirm_tx), [`rf_scan`](docs/tools.md#rf_scan), [`vanity_apply`](docs/tools.md#vanity_apply), [`vanity_grind_poll`](docs/tools.md#vanity_grind_poll), [`vanity_grind_start`](docs/tools.md#vanity_grind_start), [`vanity_grind_stop`](docs/tools.md#vanity_grind_stop), [`vanity_preview`](docs/tools.md#vanity_preview) |
| `sdk` | [`sdk_device_info`](docs/tools.md#sdk_device_info), [`sdk_list_nodes`](docs/tools.md#sdk_list_nodes), [`sdk_send_text`](docs/tools.md#sdk_send_text), [`sdk_status`](docs/tools.md#sdk_status) |
| `legacy` | [`device_info`](docs/tools.md#device_info), [`get_channel_url`](docs/tools.md#get_channel_url), [`get_config`](docs/tools.md#get_config), [`list_nodes`](docs/tools.md#list_nodes), [`send_text`](docs/tools.md#send_text), [`set_config`](docs/tools.md#set_config), [`set_debug_log_api`](docs/tools.md#set_debug_log_api), [`set_owner`](docs/tools.md#set_owner) |
<!-- END GENERATED TOOLSETS -->
## Destructive tools ask first

Tools that reboot, erase, reflash or rewrite a board take `confirm`. Without it, a client that
supports elicitation asks the human (`Allow: <action>?`), and a client that cannot ask gets a
refusal naming `confirm=True`. `MESHTASTIC_MCP_CONFIRM=human` makes the irreversible ones
(`factory_reset`, `erase_and_flash`, `nrf52_factory_erase`, `vanity_apply`, the destructive vendor
calls) ask even when the agent passes `confirm=True`. See
[docs/confirmations.md](docs/confirmations.md).

## Builds and flashes

`build_start`/`build_poll` and `flash_start`/`flash_poll` return a `job_id` at once and poll it,
which suits any client. `build`, `pio_flash`, `erase_and_flash`, `update_flash` and
`nrf52_flash_uf2` run the same jobs in one call: with the `[tasks]` extra, a client that supports
MCP tasks gets a background task, and other clients wait for the job to end. A failure is a tool
error carrying the output tail. A flash cannot be canceled; its `job_id`, in the result and in
the log line the tool sends before it waits, follows it with `flash_poll`. See
[docs/background-jobs.md](docs/background-jobs.md), and
[docs/firmware-builds.md](docs/firmware-builds.md) for each tool's arguments.

## nRF52: UF2 flashing and recovery

Boards on the Meshtastic nRF52 bootloader show a USB drive in UF2 mode. Copying a `.uf2` onto it
is several times faster than serial DFU and needs no 1200 baud touch:

```
nrf52_enter_uf2(port=P)                         # reboot into the UF2 drive (asks first)
nrf52_bootloader_info(port=P)                   # bootloader version, board, SoftDevice, Last-Fault
nrf52_backup_firmware(port=P)                   # copy CURRENT.UF2 into the data dir
nrf52_flash_uf2(uf2_path="fw.uf2", port=P)      # refuses a non-UF2 or wrong-SoftDevice image
nrf52_factory_erase(port=P)                     # wipe saved data, keep the firmware
```

Pass the board's app port, preferably its `/dev/serial/by-id/` path. The drive is matched by USB
serial, and the by-id name still carries that serial after the link disappears in UF2 mode. The
tools read the drive where the OS mounts it, so on a host that does not automount, mount it
first. `nrf52_factory_erase` needs a bootloader whose `INFO_UF2.TXT` has a `Factory-Erase:` line.

Serial DFU still works. `nrfutil_dfu(port, package_path)` does the 1200 baud touch itself,
dropping DTR as the nRF52 core needs, and fails unless adafruit-nrfutil printed
`Device programmed.`. A board left sitting in serial DFU takes the package with no touch:

```
nrfutil_raw(args=["dfu", "serial", "--package", "fw.zip", "--port", "/dev/ttyACM0",
                  "-b", "115200", "--singlebank"], confirm=True)
```

A board whose USB serial has wedged fails the call within its timeout with a hint to power-cycle
it; `uhubctl_cycle` on its hub port brings it back.

## Serve a shared bench

`meshtastic-mcp serve --host 0.0.0.0 --port 8765` serves the tools over HTTP behind a required
bearer token (`MESHTASTIC_MCP_TOKEN` or `MESHTASTIC_MCP_TOKEN_FILE`), so every client on a bench
shares one owner of the serial ports, the recorder and the jobs. See [docs/serve.md](docs/serve.md).

## Trim the tool list

`MESHTASTIC_MCP_TOOLSETS=core,firmware` exposes only the named groups, and
`MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state. The groups are `core` (the
portable device set), `firmware`, `hardware`, `android`, `apple`, `discord`, `replay`, `rf`,
`local-model` and `sdk`. A capability decides whether a tool registers and a toolset decides
whether a session lists it, so the vendor flash tools and the `nrf52_*` tools register with the
core capability but list under `hardware`. See [docs/toolsets.md](docs/toolsets.md).

## Hardware-free e2e (emulator + TCP)

Portduino `meshtasticd` native nodes run as TCP daemons (`:4403`) and mesh over UDP
multicast (`224.0.0.69:4403`) with **no LoRa hardware**. The Android emulator connects over
TCP to `10.0.2.2:<port>` as its DUT radio; the MCP server connects to other native nodes as
testers. Every e2e loop — inbound/outbound message, node-sync, config-writeback, resilience —
runs in software, CI-able on Linux runners. See `src/meshtastic_mcp/emulator/native_node.py`
and the bundled `meshtastic-e2e` skill.

## Replay — a simulated Meshtastic device (the inverse of the recorder)

Where the recorder *subscribes* to a live mesh and writes packets out, **replay** *serves* a
capture as a fake radio over TCP. An app (or AVD at `10.0.2.2:<port>`) connects to the listen
port, does the want-config handshake, and receives a paced packet stream restamped to "now" —
behaving like a radio sitting in the mesh. Useful for app/UI development and testing with zero
hardware and a fully controllable, reproducible mesh.

```python
replay_start(source="meshcon")  # generated synthetic mesh (no file)
replay_start(source="capture.db", speed=30)  # SQLite capture (full-fidelity payloads)
replay_start(source="capture.db", duration=150)  # whole capture in 2.5 min (stress test)
# or from the shell (foreground, mDNS-advertised so apps auto-discover it):
#   meshtastic-mcp replay conference-stress --nodes 1600 --rate 140 --loop
replay_start(
    source="event.db",
    channels=[  # split by OTA hash + decrypt
        {"name": "Primary", "psk": "AQ==", "primary": True},
        {"name": "Secret", "psk": "<base64-key>"},
        {"name": "Unknown", "catch_all": True},
    ],
)
replay_start(source="defcon", sim_profile={"bots": {"count": 17}})  # BBS bots + tapback storms
replay_start(source="meshcon", fuzz="adversary")  # lace it with bad actors
replay_start(source="capture.db", announce_interval=30)  # in-app "Replay Clock" progress
replay_inject(
    sid, "waypoint", {"lat": 37, "lon": -122, "geofence_radius": 500, "notify_on_enter": True}
)
replay_status()
replay_stop()
```

- **Sources** (`replay/capture.py`): SQLite captures (`*.db`/`.gz`, the Burning Man / DEF CON /
  MeshCon schema), the recorder's own `packets.jsonl`, or an in-memory synthetic mesh.
- **Pacing & discovery**: `duration=N` streams the whole windowed capture in exactly N seconds
  (whatever its packet count); else `rate` (steady pkts/sec); else `speed` (cadence multiplier).
  Pacing is drift-free (deadline-anchored), and `replay_status` reports `target_rate` vs the live
  `achieved_rate`, so a stress run is self-verifying. Sessions advertise over mDNS/Bonjour as
  `_meshtastic._tcp` with firmware-style TXT records, so apps auto-discover them in network
  discovery (no manual IP entry; `mdns=False` to opt out). `meshtastic-mcp replay` serves all of
  this from the shell — the `conference-stress` preset is the batteries-included stress scenario
  (gateway-observed density + the bot plane below).
- **Multi-channel captures**: pass a `channels` list (name + PSK, optional explicit OTA hashes,
  optional `catch_all` bucket) to route packets into the real channels by their OTA channel hash
  and advertise the keys so a connecting app shows the true channels and live-decrypts the
  encrypted half. The channel set is caller-supplied data — e.g. for an event capture you provide
  that event's channel names + PSKs; nothing event-specific is baked in.
- **Synthetic mesh** (`replay/sim.py`): seeded, PII-free *MeshCon* generator — tunable node
  count (default 800, scales to thousands) / channels / duration, a diurnal activity envelope, and
  every portnum/flavor (incl. RANGE_TEST). Its default distributions (hardware/role mix, telemetry
  value ranges, position precision, hop-limit spread, text rate, **node presence/churn** — a
  persistent core plus transient attendees, which reproduces the real heavy activity skew —
  short-message lengths, and a share of encrypted/foreign traffic) are informed by the aggregate
  statistics of real ~1,800-node captures (Burning Man + DEF CON 33) — proportions only; every
  identity, position, and message is generated. `sim.fit_profile(capture)` derives such a profile
  from any capture. Traceroutes are emitted as request→response pairs with firmware `RouteDiscovery`
  semantics so app traceroute logs populate. **Node identity**: richer names via the optional
  `[sim]` extra (Faker person names / usernames / callsigns; falls back to built-in pools),
  `emoji_short_fraction` of nodes with single-emoji short names, and **2.8 signed node data** —
  `pki_fraction` advertise a 32-byte `public_key`, a `key_verified_fraction` slice manually
  verified — so the app shows keys/verification like real 2.8 firmware.
- **BBS/bot plane** (`sim_profile={"bots": {"count": N}}`, off by default) — a meshing-around-style
  scene: auto-reply bots (ping→pong pile-ons, `cmd`/`motd`/`wx`/`joke`/games menus) egged on by
  attendees, tapback (emoji-reaction) storms incl. one legendary 100+-reaction broadcast, per-bot
  beacons, and attendee→bot traceroute pairs. Layered on after the RF-observer stage so the
  app-facing exchanges arrive intact.
- **Live injection** (`replay_inject`): push exact packets into a running session to drive app
  features on demand — a waypoint with a geofence, a node position crossing it, a text, a NodeInfo,
  or `raw`. Builders (`replay/build.py`) set proto fields the bundled lib predates (e.g. geofence)
  via raw-wire append; `fuzz=True` injects a deliberately malformed packet (shares the fuzzer's
  emit path). `capture.from_events([…])` turns a scripted scenario into a replayable capture.
  (Validated end-to-end against the Android waypoint-geofence PR.)
- **App-facing polish**: the connected node is placed at the capture's median position (sane map
  + distances); `announce_interval` adds a "Replay Clock" node posting kickoff + live ETA/progress
  to the busiest channel; `local_metrics_interval` makes the connected node report its own
  telemetry every N seconds (default 5 min) — both `DEVICE_METRICS` (battery/voltage/channel-util/
  air-util/uptime → the app's "Device Metrics" view) and `LOCAL_STATS` (packets rx/tx, online/total
  nodes, noise floor → the "Local Stats" view) — with channel-util and packets-rx tracking the live
  load;
  `modem_preset` / `firmware_edition` set the advertised LoRa preset and the app's event banner
  (e.g. `DEFCON`, `HAMVENTION`); `replay_status` returns `connect` host:port hints; a send timeout
  keeps a stalled app from hanging a session.
- **Fuzzer** (`replay/fuzz.py`, `replay_start(fuzz=…)`, `replay_fuzz_presets`): turn the stream
  hostile to test decoder + UI robustness. *Protocol* faults (corrupt/garbage/truncated payloads,
  portnum↔body mismatch, invalid-UTF-8 text, impossible telemetry, teleporting positions, hop
  anomalies, drop/duplicate) and *bad-actor campaigns* (evil-twin impersonation, flooding, GPS
  spoofing, forged ACKs, rogue ADMIN reboot/factory-reset, malicious waypoints). Seeded so a
  crash reproduces; activity surfaces under `fuzz` in `replay_status`. Presets: `light`,
  `parser`, `adversary`, `chaos`.

Every `replay_start` parameter, the `sim_profile` keys and the inject tools are in
[docs/replay.md](docs/replay.md).

## Bundled skills

Three skills ship in the wheel (`meshtastic-mcp skills install` copies them to your skills dir):
- **`meshtastic-device-ops`** — discover/connect/configure/observe/recover/flash via the MCP
  tool surface (the non-e2e workflows).
- **`meshtastic-e2e`** — cross-plane (device + app) testing: per-loop references, a verified
  device-plane helper (`mesh_e2e.py`), **journey-driven UI** (`references/journeys/`), triage,
  and the vision-oracle fallback.
- **`meshtastic-org-knowledge`** — answer questions that span the Meshtastic GitHub org:
  which project does X, status of Y, where Z is documented, what changed recently.

## FleetSuite — web control plane for a device bench

A local web UI for running a multi-board hardware bench: live device registry (discovery +
auto-enrichment with firmware/hw/region), build queue + flash, an escalating recovery ladder
(reboot → USB power-cycle → bootloader → reflash), per-device camera streams for screen
assertions, the tiered test runner, serial monitors, and optional Datadog log/metric shipping.

```bash
uv tool install 'meshtastic-mcp[web]'
meshtastic-mcp-web              # desktop window at http://127.0.0.1:8765
meshtastic-mcp-web --browser    # serve only (headless / open it yourself)
```

Binds to `127.0.0.1` by default. Destructive actions (reflash / factory-reset) require an
explicit confirmation. While FleetSuite runs it owns the bench's serial ports (monitors +
enrichment) — pause it or use its own controls rather than pointing a second tool at the same
ports; its test runner already suspends the monitors for the duration of a run. From a source checkout, `./scripts/fleetsuite.sh` bootstraps
everything (venv + npm + SPA build) in one command; `./scripts/web-dev.sh` runs the
backend + Vite dev server with HMR. Point `MESHTASTIC_FIRMWARE_ROOT` at a firmware checkout
to enable builds, reflash recovery, and exact per-board PlatformIO env resolution — without
it FleetSuite still discovers, enriches, and drives devices.

## Hardware test suite

`tests/` is a tiered pytest suite (`unit`, `mesh`, `telemetry`, `monitor`, `recovery`, `ui`,
`fleet`, `admin`, `provisioning`). `unit` runs with no hardware; the hardware tiers target a
USB-hub bench with per-board roles keyed by hub slot (reference bench: T-Echo, Heltec T114,
RAK4631, ESP32-S3 — see `tests/_bench.py` and [tests/README.md](tests/README.md)). Drive it
via `run-tests.sh`, the `meshtastic-mcp-test-tui` terminal UI, or FleetSuite's test runner.
**Setting up your own bench** (any boards, any hub): [docs/bench-setup.md](docs/bench-setup.md).

## License

GPL-3.0-only.
