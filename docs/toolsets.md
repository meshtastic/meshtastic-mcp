# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset or `all` exposes every group but `legacy`, which comes only when named (`all,legacy`); a `-group` entry drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

| Group | Tools |
| --- | --- |
| `core` | The portable device set: `devices`, `list_devices`, `inspect`, owner, config and channel writes, config snapshots, `send`, `reboot`, `shutdown`, `factory_reset`, the recorder tools, the display mirror (`capture_display`, `drive_display_*`, `send_input_event`), `inject_frame`, `triage_bundle` and `doctor`. |
| `legacy` | The pre-1.0 tools that `next` tools replace, listed in [tools.md](tools.md#legacy). Off unless named; removed in 1.1. |
| `firmware` | Build, flash, boards and userPrefs. Registered only with `MESHTASTIC_FIRMWARE_ROOT`. |
| `hardware` | esptool, nrfutil, picotool, uhubctl, `touch_1200bps`, the `nrf52_*` UF2 bootloader tools, the `serial_*` monitor sessions (they run `pio device monitor` in the firmware tree) and `capture_screen` (a webcam and the `[ui]` extra). |
| `android` | Android emulator and app driving (`android_*`), the ATAK fleet (`atak_*`). |
| `apple` | iOS Simulator driving (`ios_*`) and `screenshot_set_*`. |
| `app` | `app_view` and `app`: one entry point over the Android, iOS and desktop app drivers. Always registered; desktop actions need `MESHTASTIC_MCP_DESKTOP_URL`. |
| `replay` | The simulated device (`replay_*`, `inject`) and the CoT relay (`cot_relay_*`). |
| `rf` | The SDR oracle (`rf_*`), the power meter (`pa_*`), the BLE sniffer (`ble_sniff_*`) and vanity keys (`vanity_*`). |
| `sdk` | The experimental Kotlin SDK bridge (`sdk_*`). |

`core` holds only tools that need the base install and a radio over serial or TCP. A test pins it to an allowlist, so a new tool must name its group. `screenshot_set_*` registers with the android or the apple capability and lives in `apple`, so an Android-only session adds `apple` to get it. An unknown name stops the server at startup and lists the valid ones. The `discord_*` tools are not a group here; the separate `meshtastic-mcp-discord` server serves them ([docs/discord.md](discord.md)).

`core` alone is about a quarter of the full list's size, `READ_ONLY` a little under half, and both together about an eighth. The capabilities active on the host decide the full list; your client's tool list shows what a setting leaves.

```bash
claude mcp add meshtastic -e MESHTASTIC_MCP_TOOLSETS=core,firmware -e MESHTASTIC_MCP_READ_ONLY=1 -- meshtastic-mcp
```

To flash and recover boards, add `hardware`: `MESHTASTIC_MCP_TOOLSETS=core,firmware,hardware`.
