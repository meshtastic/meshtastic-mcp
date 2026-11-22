# meshtastic-mcp

<!-- mcp-name: io.github.meshtastic/meshtastic-mcp -->

[![CI](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent discover, configure,
observe, flash and test Meshtastic radios and the apps that talk to them. It reads and changes
device config, sends mesh traffic, watches live packets and logs, builds and flashes firmware,
drives the Android and Apple apps, and serves a simulated radio for hardware-free tests. A
bundled skill set covers closed-loop device and app end-to-end testing. Core tools work against
any serial, TCP or BLE radio with no firmware checkout.

## Quick start (60 seconds)

Plug in a radio, then install and register the server with Claude Code:

```bash
uv tool install 'meshtastic-mcp[ui]'
claude mcp add meshtastic -s user -- meshtastic-mcp
meshtastic-mcp doctor          # check what is available on this machine
```

Restart the client, then ask it to make three calls:

```text
devices()
inspect(node="heltec-v3-aa13", what=["lora", "nodes"])
configure(node="heltec-v3-aa13", changes={"lora.region": "US"}, dry_run=True)
```

`devices` lists the attached radios by name, and those names go in every `node` argument.
`inspect` reads a radio. `configure` previews the change with `dry_run=True`; without it, the
call writes the change and reads it back. Both calls and every other tool are in
[docs/tools.md](docs/tools.md).

## Install

Claude Code:

```bash
uv tool install 'meshtastic-mcp[ui]'
claude mcp add meshtastic -s user -- meshtastic-mcp
```

Any other MCP client takes a stdio server entry:

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

`meshtastic-mcp install --client claude-code|claude-desktop|cursor|windsurf` writes a stdio entry
into the client's config for you, and `--print` shows it without editing anything. Extras, the
other clients, updating, source checkouts and a shared bench over HTTP are in
[docs/install.md](docs/install.md).

## Tools

The tool list is generated from the registered tools. Each tool's arguments and result are in
[docs/tools.md](docs/tools.md).

<!-- BEGIN GENERATED TOOLSETS: `meshtastic-mcp docs generate` -->
| Toolset | Tools |
| --- | --- |
| `core` | [`admin`](docs/tools.md#admin), [`claim`](docs/tools.md#claim), [`config_diff`](docs/tools.md#config_diff), [`config_snapshot`](docs/tools.md#config_snapshot), [`config_snapshots_list`](docs/tools.md#config_snapshots_list), [`configure`](docs/tools.md#configure), [`devices`](docs/tools.md#devices), [`doctor`](docs/tools.md#doctor), [`inject_frame`](docs/tools.md#inject_frame), [`inspect`](docs/tools.md#inspect), [`job`](docs/tools.md#job), [`mark_event`](docs/tools.md#mark_event), [`query`](docs/tools.md#query), [`recorder_export`](docs/tools.md#recorder_export), [`recorder_pause`](docs/tools.md#recorder_pause), [`recorder_resume`](docs/tools.md#recorder_resume), [`recorder_status`](docs/tools.md#recorder_status), [`release`](docs/tools.md#release), [`send`](docs/tools.md#send), [`set_channel_url`](docs/tools.md#set_channel_url), [`wait_for`](docs/tools.md#wait_for) |
| `android` | [`android_docs_fetch`](docs/tools.md#android_docs_fetch), [`android_docs_search`](docs/tools.md#android_docs_search), [`android_render_compose_preview`](docs/tools.md#android_render_compose_preview), [`android_version_lookup`](docs/tools.md#android_version_lookup) |
| `app` | [`app`](docs/tools.md#app), [`app_view`](docs/tools.md#app_view) |
| `apple` | [`ios_list_simulators`](docs/tools.md#ios_list_simulators), [`ios_screenshot`](docs/tools.md#ios_screenshot), [`screenshot_set_start`](docs/tools.md#screenshot_set_start) |
| `display` | [`capture_display`](docs/tools.md#capture_display), [`capture_screen`](docs/tools.md#capture_screen), [`drive_display_start`](docs/tools.md#drive_display_start), [`send_input_event`](docs/tools.md#send_input_event) |
| `firmware` | [`build`](docs/tools.md#build), [`clean`](docs/tools.md#clean), [`get_board`](docs/tools.md#get_board), [`list_boards`](docs/tools.md#list_boards), [`push_fake_nodedb`](docs/tools.md#push_fake_nodedb), [`userprefs_get`](docs/tools.md#userprefs_get), [`userprefs_manifest`](docs/tools.md#userprefs_manifest), [`userprefs_reset`](docs/tools.md#userprefs_reset), [`userprefs_set`](docs/tools.md#userprefs_set), [`userprefs_testing_profile`](docs/tools.md#userprefs_testing_profile) |
| `hardware` | [`install`](docs/tools.md#install), [`nrf52_backup_firmware`](docs/tools.md#nrf52_backup_firmware), [`nrf52_bootloader_info`](docs/tools.md#nrf52_bootloader_info), [`nrf52_eject_uf2`](docs/tools.md#nrf52_eject_uf2), [`nrf52_enter_uf2`](docs/tools.md#nrf52_enter_uf2), [`nrf52_factory_erase`](docs/tools.md#nrf52_factory_erase), [`nrf52_mount_uf2`](docs/tools.md#nrf52_mount_uf2), [`serial_close`](docs/tools.md#serial_close), [`serial_open`](docs/tools.md#serial_open), [`touch_1200bps`](docs/tools.md#touch_1200bps), [`uhubctl_cycle`](docs/tools.md#uhubctl_cycle), [`uhubctl_list`](docs/tools.md#uhubctl_list), [`uhubctl_power`](docs/tools.md#uhubctl_power), [`vendor`](docs/tools.md#vendor) |
| `replay` | [`inject`](docs/tools.md#inject), [`sim`](docs/tools.md#sim), [`sim_status`](docs/tools.md#sim_status) |
| `rf` | [`ble_sniff_start`](docs/tools.md#ble_sniff_start), [`ble_sniff_status`](docs/tools.md#ble_sniff_status), [`ble_sniff_stop`](docs/tools.md#ble_sniff_stop), [`pa_measure`](docs/tools.md#pa_measure), [`pa_meter_status`](docs/tools.md#pa_meter_status), [`pa_sweep`](docs/tools.md#pa_sweep), [`rf_confirm_tx`](docs/tools.md#rf_confirm_tx), [`rf_scan`](docs/tools.md#rf_scan), [`vanity_apply`](docs/tools.md#vanity_apply), [`vanity_grind_start`](docs/tools.md#vanity_grind_start), [`vanity_grind_stop`](docs/tools.md#vanity_grind_stop), [`vanity_preview`](docs/tools.md#vanity_preview) |
| `tak` | [`atak_drive_route`](docs/tools.md#atak_drive_route), [`atak_drive_stop`](docs/tools.md#atak_drive_stop), [`atak_fleet_down`](docs/tools.md#atak_fleet_down), [`atak_fleet_status`](docs/tools.md#atak_fleet_status), [`atak_fleet_up`](docs/tools.md#atak_fleet_up), [`atak_share_item`](docs/tools.md#atak_share_item), [`cot_relay_start`](docs/tools.md#cot_relay_start), [`cot_relay_status`](docs/tools.md#cot_relay_status), [`cot_relay_stop`](docs/tools.md#cot_relay_stop) |
| `legacy` | [`android_clear_logcat`](docs/tools.md#android_clear_logcat), [`android_find_text`](docs/tools.md#android_find_text), [`android_poll_for_text`](docs/tools.md#android_poll_for_text), [`android_read_logcat`](docs/tools.md#android_read_logcat), [`android_resolve`](docs/tools.md#android_resolve), [`android_screenshot`](docs/tools.md#android_screenshot), [`android_swipe`](docs/tools.md#android_swipe), [`android_tap`](docs/tools.md#android_tap), [`android_type_text`](docs/tools.md#android_type_text), [`android_ui_dump`](docs/tools.md#android_ui_dump), [`ble_sniff_poll`](docs/tools.md#ble_sniff_poll), [`build_poll`](docs/tools.md#build_poll), [`build_start`](docs/tools.md#build_start), [`device_info`](docs/tools.md#device_info), [`drive_display_poll`](docs/tools.md#drive_display_poll), [`erase_and_flash`](docs/tools.md#erase_and_flash), [`esptool_chip_info`](docs/tools.md#esptool_chip_info), [`esptool_erase_flash`](docs/tools.md#esptool_erase_flash), [`esptool_raw`](docs/tools.md#esptool_raw), [`events_window`](docs/tools.md#events_window), [`factory_reset`](docs/tools.md#factory_reset), [`flash_poll`](docs/tools.md#flash_poll), [`flash_start`](docs/tools.md#flash_start), [`get_channel_url`](docs/tools.md#get_channel_url), [`get_config`](docs/tools.md#get_config), [`ios_boot`](docs/tools.md#ios_boot), [`ios_open_url`](docs/tools.md#ios_open_url), [`ios_tap`](docs/tools.md#ios_tap), [`ios_type_text`](docs/tools.md#ios_type_text), [`ios_ui_dump`](docs/tools.md#ios_ui_dump), [`list_devices`](docs/tools.md#list_devices), [`list_nodes`](docs/tools.md#list_nodes), [`logs_window`](docs/tools.md#logs_window), [`nrf52_flash_uf2`](docs/tools.md#nrf52_flash_uf2), [`nrfutil_dfu`](docs/tools.md#nrfutil_dfu), [`nrfutil_raw`](docs/tools.md#nrfutil_raw), [`packets_window`](docs/tools.md#packets_window), [`picotool_info`](docs/tools.md#picotool_info), [`picotool_load`](docs/tools.md#picotool_load), [`picotool_raw`](docs/tools.md#picotool_raw), [`pio_flash`](docs/tools.md#pio_flash), [`reboot`](docs/tools.md#reboot), [`replay_fuzz_presets`](docs/tools.md#replay_fuzz_presets), [`replay_inject`](docs/tools.md#replay_inject), [`replay_inject_beacon`](docs/tools.md#replay_inject_beacon), [`replay_inject_client_notification`](docs/tools.md#replay_inject_client_notification), [`replay_inject_fileinfo`](docs/tools.md#replay_inject_fileinfo), [`replay_inject_traceroute`](docs/tools.md#replay_inject_traceroute), [`replay_inject_waypoint`](docs/tools.md#replay_inject_waypoint), [`replay_start`](docs/tools.md#replay_start), [`replay_status`](docs/tools.md#replay_status), [`replay_stop`](docs/tools.md#replay_stop), [`screenshot_set_poll`](docs/tools.md#screenshot_set_poll), [`sdk_device_info`](docs/tools.md#sdk_device_info), [`sdk_list_nodes`](docs/tools.md#sdk_list_nodes), [`sdk_send_text`](docs/tools.md#sdk_send_text), [`sdk_status`](docs/tools.md#sdk_status), [`send_text`](docs/tools.md#send_text), [`serial_list`](docs/tools.md#serial_list), [`serial_read`](docs/tools.md#serial_read), [`set_config`](docs/tools.md#set_config), [`set_debug_log_api`](docs/tools.md#set_debug_log_api), [`set_owner`](docs/tools.md#set_owner), [`shutdown`](docs/tools.md#shutdown), [`telemetry_timeline`](docs/tools.md#telemetry_timeline), [`triage_bundle`](docs/tools.md#triage_bundle), [`update_flash`](docs/tools.md#update_flash), [`vanity_grind_poll`](docs/tools.md#vanity_grind_poll) |
<!-- END GENERATED TOOLSETS -->

Tools belong to toolsets, and a capability decides whether a tool registers at all. A hidden
tool is not listed and cannot be called. [docs/toolsets.md](docs/toolsets.md) has the groups and
the prerequisite for each capability.

The device tools are `devices`, `inspect`, `configure`, `send`, `admin`, `query`, `wait_for`,
`job`, `claim`, `release`, `install` and `vendor`. The pre-1.0 tools (`device_info`,
`get_config`, `set_config`, `send_text`, `reboot` and the rest) are an opt-in `legacy` toolset:
`MESHTASTIC_MCP_TOOLSETS=all,legacy`. It is removed in 1.1.

Beyond tools, the server exposes MCP resources (`meshtastic://doctor`,
`meshtastic://capabilities` and the templated `meshtastic://e2e/{loop}`) and prompts
(`triage_e2e_failure`, `bringup_device`, `inbound_loop`).

## Configuration

Set these in the environment of the server process, or in the client's `env` block.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated toolsets to expose. Unset is the default groups (`core`, `app`, `hardware`, `replay`); `all` adds the specialist groups; `legacy` comes only when named; `-group` drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that can change state. |
| `MESHTASTIC_MCP_CONFIRM` | `agent` (default) or `human`. `human` makes irreversible tools ask a person even when the agent passes `confirm=True`. |
| `MESHTASTIC_FIRMWARE_ROOT` | A firmware checkout. Registers the `firmware` toolset (build, flash, boards, userPrefs). |
| `MESHTASTIC_MCP_DATA_DIR` | Where the server keeps leases, device names, jobs, logs and snapshots. Defaults to the platform user-data directory. |
| `MESHTASTIC_MCP_BACKEND` | `python` or `sdk`. A `ble:` node always uses the SDK backend. |
| `MESHTASTIC_MCP_STANDALONE` | `1` makes a stdio client run its own server instead of bridging to a running `serve`. |

`MESHTASTIC_ANDROID_ROOT` and `MESHTASTIC_APPLE_ROOT` point the Android and Apple tools at
source checkouts. `meshtastic-mcp doctor` reports which prerequisites are present and prints the
command to fix each missing one.

## Security

- Tools that reboot, erase, reflash or rewrite a board take `confirm`. A client that can show a
  prompt asks the human; one that cannot gets a refusal naming `confirm=True`.
- `MESHTASTIC_MCP_CONFIRM=human` makes the irreversible tools ask a person even with
  `confirm=True`. Over HTTP the prompt is not a human gate, so the bearer token is the boundary.
- `claim` leases a radio so other sessions cannot use it, and `MESHTASTIC_MCP_READ_ONLY=1` removes
  every write tool.
- Mesh text, app UI, logcat and Discord messages are untrusted input that can carry
  instructions aimed at the agent.

The full model is in [docs/safety.md](docs/safety.md). Report a vulnerability as
[SECURITY.md](SECURITY.md) describes.

## Documentation

Start here:

- [docs/install.md](docs/install.md): every client, extras, updating, source checkouts
- [docs/tools.md](docs/tools.md): the generated tool reference
- [docs/toolsets.md](docs/toolsets.md): toolsets, capabilities and trimming the tool list
- [docs/safety.md](docs/safety.md): confirmations, leases, read-only mode, jobs, prompt injection
- [docs/troubleshooting.md](docs/troubleshooting.md): symptom, cause, fix
- [docs/serve.md](docs/serve.md): one shared server for a bench

Firmware and hardware:

- [docs/firmware-builds.md](docs/firmware-builds.md): build, flash, userPrefs, nRF52 UF2 flashing and recovery
- [docs/bench-setup.md](docs/bench-setup.md): your own hardware bench, FleetSuite and the hardware test suite
- [docs/power-meter.md](docs/power-meter.md), [docs/rf-oracle.md](docs/rf-oracle.md), [docs/ble-sniffer.md](docs/ble-sniffer.md), [docs/vanity.md](docs/vanity.md)
- [docs/display-mirror.md](docs/display-mirror.md), [docs/screenshot-set.md](docs/screenshot-set.md)

End-to-end testing:

- [docs/replay.md](docs/replay.md): a simulated radio, the synthetic mesh, the fuzzer and live injection
- [docs/atak-cot.md](docs/atak-cot.md), [docs/atak-cot-emission.md](docs/atak-cot-emission.md)
- [docs/sdk-cli-bridge.md](docs/sdk-cli-bridge.md), [docs/nightly.md](docs/nightly.md)

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Release history is in
[CHANGELOG.md](CHANGELOG.md).

## License

GPL-3.0-only.
