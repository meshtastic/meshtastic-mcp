# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset exposes the default groups (`core`, `app`, `hardware`, `replay`, about 40 tools). `all` adds every specialist group; `legacy` comes only when named (`all,legacy`). A `-group` entry drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

| Group | Default | Tools |
| --- | --- | --- |
| `core` | yes | The device intents (`devices`, `inspect`, `configure`, `send`, `admin`, `set_channel_url`), leases (`claim`, `release`), long work (`job`), the recorder (`query`, `wait_for`, `mark_event`, `recorder_*`), config snapshots, `inject_frame` and `doctor`. |
| `app` | yes | `app` and `app_view`: one entry point over the Android, iOS and desktop app drivers. Desktop actions need `MESHTASTIC_MCP_DESKTOP_URL`. |
| `hardware` | yes | `install` and `vendor`, the `nrf52_*` UF2 bootloader tools, `serial_open`/`serial_close` (their output lands in the recorder for `query`), `touch_1200bps` and uhubctl. |
| `replay` | yes | The simulated radio: `sim`, `sim_status` and `inject`. |
| `firmware` | with `all` | `build`, `clean`, boards, userPrefs and `push_fake_nodedb`. Registered only with `MESHTASTIC_FIRMWARE_ROOT`; `install` builds without this group. |
| `display` | with `all` | The device's own screen: `capture_display`, `drive_display_start`, `send_input_event`, and the webcam `capture_screen` (the `[ui]` extra). |
| `android` | with `all` | Android tools `app` does not cover: docs search, Compose preview rendering, version lookup. |
| `apple` | with `all` | iOS tools `app` does not cover: `ios_screenshot` (appearance and status bar), `ios_list_simulators`, `screenshot_set_start`. |
| `tak` | with `all` | The ATAK emulator fleet (`atak_*`) and the CoT relay (`cot_relay_*`). |
| `rf` | with `all` | The SDR oracle (`rf_*`), the power meter (`pa_*`), the BLE sniffer (`ble_sniff_*`) and vanity keys (`vanity_*`). |
| `sdk` | with `all` | Nothing of its own now; the SDK backend runs under `inspect`, `send` and `admin` with `MESHTASTIC_MCP_BACKEND=sdk`. |
| `legacy` | when named | The pre-1.0 tools a `next` tool replaces, listed in [tools.md](tools.md#legacy). A call to one by name while it is hidden returns the call that replaces it. |

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
