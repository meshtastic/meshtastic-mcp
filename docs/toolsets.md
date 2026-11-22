# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset exposes the groups marked "by default" below. `all` adds every specialist group; `legacy` comes only when named (`all,legacy`). A `-group` entry drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

<!-- BEGIN GENERATED GROUPS: `meshtastic-mcp docs generate` -->
| Group | Exposed | What it holds | Tools |
| --- | --- | --- | --- |
| `core` | by default | The device intents, leases, long work, the recorder and its queries, config snapshots, frame injection and `doctor`. Needs only the base install and a radio. | [`admin`](tools.md#admin), [`claim`](tools.md#claim), [`config_diff`](tools.md#config_diff), [`config_snapshot`](tools.md#config_snapshot), [`config_snapshots_list`](tools.md#config_snapshots_list), [`configure`](tools.md#configure), [`devices`](tools.md#devices), [`doctor`](tools.md#doctor), [`inject_frame`](tools.md#inject_frame), [`inspect`](tools.md#inspect), [`job`](tools.md#job), [`mark_event`](tools.md#mark_event), [`query`](tools.md#query), [`recorder_export`](tools.md#recorder_export), [`recorder_pause`](tools.md#recorder_pause), [`recorder_resume`](tools.md#recorder_resume), [`recorder_status`](tools.md#recorder_status), [`release`](tools.md#release), [`send`](tools.md#send), [`set_channel_url`](tools.md#set_channel_url), [`wait_for`](tools.md#wait_for) |
| `android` | with `all` | Android tools `app` does not cover. | [`android_docs_fetch`](tools.md#android_docs_fetch), [`android_docs_search`](tools.md#android_docs_search), [`android_render_compose_preview`](tools.md#android_render_compose_preview), [`android_version_lookup`](tools.md#android_version_lookup) |
| `app` | by default | One entry point over the Android, iOS and desktop app drivers. Desktop actions need `MESHTASTIC_MCP_DESKTOP_URL`. | [`app`](tools.md#app), [`app_view`](tools.md#app_view) |
| `apple` | with `all` | iOS tools `app` does not cover, and matching Android and iOS screenshot sets. | [`ios_list_simulators`](tools.md#ios_list_simulators), [`ios_screenshot`](tools.md#ios_screenshot), [`screenshot_set_start`](tools.md#screenshot_set_start) |
| `display` | with `all` | The device's own screen, and the webcam (the `[ui]` extra). | [`capture_display`](tools.md#capture_display), [`capture_screen`](tools.md#capture_screen), [`drive_display_start`](tools.md#drive_display_start), [`send_input_event`](tools.md#send_input_event) |
| `firmware` | with `all` | Builds, boards and userPrefs from a firmware tree. Registered only with `MESHTASTIC_FIRMWARE_ROOT`; `install` builds without this group. | [`build`](tools.md#build), [`clean`](tools.md#clean), [`get_board`](tools.md#get_board), [`list_boards`](tools.md#list_boards), [`push_fake_nodedb`](tools.md#push_fake_nodedb), [`userprefs_get`](tools.md#userprefs_get), [`userprefs_manifest`](tools.md#userprefs_manifest), [`userprefs_reset`](tools.md#userprefs_reset), [`userprefs_set`](tools.md#userprefs_set), [`userprefs_testing_profile`](tools.md#userprefs_testing_profile) |
| `hardware` | by default | Flashing (`install`), vendor tools (`vendor`), the nRF52 UF2 bootloader, raw serial sessions that feed the recorder, and hub power. | [`install`](tools.md#install), [`nrf52_backup_firmware`](tools.md#nrf52_backup_firmware), [`nrf52_bootloader_info`](tools.md#nrf52_bootloader_info), [`nrf52_eject_uf2`](tools.md#nrf52_eject_uf2), [`nrf52_enter_uf2`](tools.md#nrf52_enter_uf2), [`nrf52_factory_erase`](tools.md#nrf52_factory_erase), [`nrf52_mount_uf2`](tools.md#nrf52_mount_uf2), [`serial_close`](tools.md#serial_close), [`serial_open`](tools.md#serial_open), [`touch_1200bps`](tools.md#touch_1200bps), [`uhubctl_cycle`](tools.md#uhubctl_cycle), [`uhubctl_list`](tools.md#uhubctl_list), [`uhubctl_power`](tools.md#uhubctl_power), [`vendor`](tools.md#vendor) |
| `replay` | by default | The simulated radio and frame injection into it. | [`inject`](tools.md#inject), [`sim`](tools.md#sim), [`sim_status`](tools.md#sim_status) |
| `rf` | with `all` | The SDR oracle, the power meter, the BLE sniffer and vanity keys. | [`ble_sniff_start`](tools.md#ble_sniff_start), [`ble_sniff_status`](tools.md#ble_sniff_status), [`ble_sniff_stop`](tools.md#ble_sniff_stop), [`pa_measure`](tools.md#pa_measure), [`pa_meter_status`](tools.md#pa_meter_status), [`pa_sweep`](tools.md#pa_sweep), [`rf_confirm_tx`](tools.md#rf_confirm_tx), [`rf_scan`](tools.md#rf_scan), [`vanity_apply`](tools.md#vanity_apply), [`vanity_grind_start`](tools.md#vanity_grind_start), [`vanity_grind_stop`](tools.md#vanity_grind_stop), [`vanity_preview`](tools.md#vanity_preview) |
| `tak` | with `all` | The ATAK emulator fleet and the CoT relay. | [`atak_drive_route`](tools.md#atak_drive_route), [`atak_drive_stop`](tools.md#atak_drive_stop), [`atak_fleet_down`](tools.md#atak_fleet_down), [`atak_fleet_status`](tools.md#atak_fleet_status), [`atak_fleet_up`](tools.md#atak_fleet_up), [`atak_share_item`](tools.md#atak_share_item), [`cot_relay_start`](tools.md#cot_relay_start), [`cot_relay_status`](tools.md#cot_relay_status), [`cot_relay_stop`](tools.md#cot_relay_stop) |
| `legacy` | when named | The pre-1.0 tools a newer tool replaces. A call to one by name while it is hidden returns the call that replaces it. | [listed in tools.md](tools.md#legacy) |
<!-- END GENERATED GROUPS -->

`core` holds only tools that need the base install and a radio over serial or TCP. A test pins it to an allowlist, so a new tool must name its group. Calling a tool from a group the server leaves out returns which setting adds it. An unknown name stops the server at startup and lists the valid ones. The `discord_*` tools are not a group here; the separate `meshtastic-mcp-discord` server serves them ([docs/discord.md](discord.md)).

```bash
claude mcp add meshtastic -e MESHTASTIC_MCP_TOOLSETS=core -e MESHTASTIC_MCP_READ_ONLY=1 -- meshtastic-mcp
```

For firmware work in the tree, add the specialist groups: `MESHTASTIC_MCP_TOOLSETS=all`, or `core,hardware,firmware`.

## Capabilities

A capability decides whether a tool registers at all, and its prerequisite must be present when the server starts. The active set is logged at startup (`meshtastic-mcp capabilities active: ...`), and `doctor` names the missing prerequisite.

| Capability | Prerequisite | Adds |
| --- | --- | --- |
| core | none | Discovery, serial, TCP and BLE transport, `inspect`, `configure`, `send`, `admin`, the recorder and its queries, replay, input events, uhubctl, esptool, nrfutil, picotool and the nRF52 UF2 tools |
| firmware | `MESHTASTIC_FIRMWARE_ROOT` and PlatformIO `pio` | Build, clean, flash, OTA, board listing and userPrefs |
| android | the `android` CLI and `adb` | Android emulator and native-node orchestration for hardware-free e2e |
| apple | `xcrun` (and `idb` for UI driving) | iOS Simulator and macOS app orchestration (`ios_*`). `screenshot_set_*` registers with this or android and captures matching screens from both, see [screenshot-set.md](screenshot-set.md) |
| sdr | the `[sdr]` extra and an RTL-SDR dongle | The RF compliance oracle, see [rf-oracle.md](rf-oracle.md) |
| tak | the meshtastic-tak SDK, which is not on PyPI: install it with the command `doctor` prints, or `uv sync --group tak` in a checkout | TAKPacketV2 zstd wire compression for the replay simulator's TAK squad (`profile tak.wire="v2"`) and its CoT stream to ATAK |
| sdk-cli (experimental) | the Kotlin SDK headless CLI | An alternate device backend, see [sdk-cli-bridge.md](sdk-cli-bridge.md) |
| discord | a read-only bot token (`$DISCORD_BOT_TOKEN` or `<user-config-dir>/meshtastic-mcp/discord.token`; `doctor` prints the path) | Read access to the Meshtastic Discord server, see [discord.md](discord.md) |
| ble-sniffer | an nRF Sniffer for Bluetooth LE dongle (Nordic VID `0x1915`, PID `0x522A`) and Nordic's `nrfutil-ble-sniffer` plugin | `ble_sniff_start`, `_poll` and `_stop` capture the phone-to-node BLE link from outside both ends. `ble_sniff_status` is always registered. See [ble-sniffer.md](ble-sniffer.md) |
| mvgrind | the `mvgrind` binary and an OpenCL driver | `vanity_grind_*` searches for a vanity node number or app color. `vanity_preview` and `vanity_apply` are always registered. See [vanity.md](vanity.md) |

On macOS, a Homebrew `librtlsdr` is the osmocom fork and lacks `rtlsdr_set_dithering`, so `import rtlsdr` fails. The `[sdr]` extra bundles `pyrtlsdrlib`, which avoids this and is preferred by the loader.

The server also exposes MCP resources (`meshtastic://doctor`, `meshtastic://capabilities`, and the templated `meshtastic://e2e/{loop}` for the bundled e2e recipes) and prompts (`triage_e2e_failure`, `bringup_device`, `inbound_loop`). A tool with a result type in `schemas.py` advertises it as an output schema, and a device, flash or hardware tool that fails returns a tool error (`isError`) carrying the cause and the tail of any command output.
