# Trimming the tool list

Every tool a client loads costs context. Two environment variables shrink what the server advertises; a hidden tool is not listed and cannot be called.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated groups to expose. Unset or `all` exposes every group. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that is not read-only. |

Groups: `core`, `firmware`, `hardware` (esptool, nrfutil, picotool, uhubctl), `android` (includes ATAK), `discord`, `replay` (includes the CoT relay), `rf` (power meter, BLE sniffer, vanity), `local-model`. An unknown name stops the server at startup and lists the valid ones.

With every capability active, the full list is 104 tools, roughly 23k tokens of schema. `core` is 37 tools (7k), `READ_ONLY` is 51 (9k), and both together are 18 (3k). Token counts are bytes divided by four, so treat them as ratios.

```bash
claude mcp add meshtastic -e MESHTASTIC_MCP_TOOLSETS=core,firmware -e MESHTASTIC_MCP_READ_ONLY=1 -- meshtastic-mcp
```
