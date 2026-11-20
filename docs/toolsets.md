# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset or `all` exposes every group but `legacy`, which comes only when named (`all,legacy`); a `-group` entry drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

| Group | Tools |
| --- | --- |
| `core` | The portable device set: `devices`, `list_devices`, `inspect`, owner, config and channel writes, config snapshots, `send_text`, `reboot`, `shutdown`, `factory_reset`, the recorder tools, the display mirror (`capture_display`, `drive_display_*`, `send_input_event`), `inject_frame`, `triage_bundle` and `doctor`. |
| `legacy` | The pre-1.0 tools `inspect` and `configure` replace: `device_info`, `list_nodes`, `get_config`, `get_channel_url`, `set_config`, `set_owner`, `set_debug_log_api`. Off unless named; removed in 1.1. |
| `firmware` | Build, flash, boards and userPrefs. Registered only with `MESHTASTIC_FIRMWARE_ROOT`. |
| `hardware` | esptool, nrfutil, picotool, uhubctl, `touch_1200bps`, the `nrf52_*` UF2 bootloader tools, the `serial_*` monitor sessions (they run `pio device monitor` in the firmware tree) and `capture_screen` (a webcam and the `[ui]` extra). |
| `android` | Android emulator and app driving (`android_*`), the ATAK fleet (`atak_*`). |
| `apple` | iOS Simulator driving (`ios_*`) and `screenshot_set_*`. |
| `discord` | `discord_*`. |
| `replay` | The simulated device (`replay_*`) and the CoT relay (`cot_relay_*`). |
| `rf` | The SDR oracle (`rf_*`), the power meter (`pa_*`), the BLE sniffer (`ble_sniff_*`) and vanity keys (`vanity_*`). |
| `local-model` | Local-model offload and its backend tools. |
| `sdk` | The experimental Kotlin SDK bridge (`sdk_*`). |

`core` holds only tools that need the base install and a radio over serial or TCP. A test pins it to an allowlist, so a new tool must name its group. `screenshot_set_*` registers with the android or the apple capability and lives in `apple`, so an Android-only session adds `apple` to get it. An unknown name stops the server at startup and lists the valid ones.

`core` alone is about a quarter of the full list's size, `READ_ONLY` a little under half, and both together about an eighth. The capabilities active on the host decide the full list; your client's tool list shows what a setting leaves.

```bash
claude mcp add meshtastic -e MESHTASTIC_MCP_TOOLSETS=core,firmware -e MESHTASTIC_MCP_READ_ONLY=1 -- meshtastic-mcp
```

To flash and recover boards, add `hardware`: `MESHTASTIC_MCP_TOOLSETS=core,firmware,hardware`.

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
