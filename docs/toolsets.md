# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset or `all` exposes every group. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

Groups: `core`, `firmware`, `hardware` (esptool, nrfutil, picotool, uhubctl, `touch_1200bps` and the `nrf52_*` UF2 bootloader tools), `android` (includes ATAK), `discord`, `replay` (includes the CoT relay), `rf` (power meter, BLE sniffer, vanity), `local-model`. A tool in no named group is `core`. An unknown name stops the server at startup and lists the valid ones.

`core` alone is about a third of the full list, `READ_ONLY` about half, and both together under a fifth. The capabilities active on the host decide the full list; your client's tool list shows what a setting leaves.

```bash
claude mcp add meshtastic -e MESHTASTIC_MCP_TOOLSETS=core,firmware -e MESHTASTIC_MCP_READ_ONLY=1 -- meshtastic-mcp
```

To flash and recover boards, add `hardware`: `MESHTASTIC_MCP_TOOLSETS=core,firmware,hardware`.
