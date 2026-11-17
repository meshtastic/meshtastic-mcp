# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories
(<https://github.com/meshtastic/meshtastic-mcp/security/advisories/new>) rather than a public
issue. We aim to acknowledge within a few days.

## Scope notes

- Every destructive device operation (`reboot`, `shutdown`, `factory_reset`, the flash and
  `nrf52_*` tools, the destructive `esptool`/`nrfutil`/`picotool` calls, `uhubctl_*` and the
  rest listed in `docs/confirmations.md`) is `confirm`-gated and `destructiveHint`-annotated.
  Treat any path that bypasses the gate as a security-relevant bug.
- `confirm=True` is an argument an agent can pass for itself. The human gate is the
  elicitation prompt the server sends when the client declared it can ask, and
  `MESHTASTIC_MCP_CONFIRM=human` makes irreversible tools ask even with `confirm=True`. Over
  `meshtastic-mcp serve` the client declares its own capabilities, so the prompt is not a
  human gate there; the bearer token is the boundary.
- Never log or transmit PII, location, or cryptographic keys.

## Prompt injection / lethal trifecta

The [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) for AI
agents is: **private data + untrusted content + exfiltration**. All three legs are present
in a full meshtastic-mcp session:

| Leg | Tools |
|---|---|
| Private data | `device_info`, `list_nodes`, `get_config`, `get_channel_url` |
| Untrusted content | `logs_window`, `packets_window` — return user-authored payloads from remote mesh nodes; `android_ui_dump`, `android_screenshot`, `android_read_logcat` — return app UI / device logcat content that can echo those same remote-node payloads (node names, text messages); `cot_relay_status` — surfaces per-peer **callsigns** supplied by connected TAK clients (the full CoT events, incl. GeoChat bodies, are written to capture files on disk, not returned by this tool); the `discord_*` tools — return messages / channel topics / member names authored by anyone on the public Meshtastic Discord server (the per-message `trust` tier is derived from public roles and is a reading aid, not authentication); `ble_sniff_poll`, `ble_sniff_stop` — return BLE device names captured off the air, which are arbitrary text chosen by whoever owns the radio broadcasting them (this is the widest of these sources: it needs no mesh membership and no app, only radio proximity) |
| Exfiltration | `send_text` — broadcasts a mesh message |

A hostile node on the same mesh could embed instructions in a packet payload. If an agent
processes that payload alongside `device_info` and has `send_text` available, an attacker
could exfiltrate the device's channel URL or node list.

**Mitigations in place:**
- `logs_window`, `packets_window`, `android_ui_dump`, `android_screenshot`,
  `android_read_logcat`, `cot_relay_status`, `ble_sniff_poll`, `ble_sniff_stop`, and the
  `discord_*` tools are classified `openWorldHint: true` so clients can detect that
  untrusted content has entered the session. (`atak_fleet_status` is also `openWorldHint` — it drives external
  emulators — but returns only fleet/route run-state, no remote-authored content.)
- `send_text` is `destructiveHint: true` — clients should prompt before broadcasting.
- Destructive ops ask the human through elicitation when the client supports it, and
  `MESHTASTIC_MCP_CONFIRM=human` keeps that prompt on irreversible ones even when the agent
  passes `confirm=True`. See `docs/confirmations.md`.

**Recommended operational posture:**
- Do not process untrusted mesh content and send text in the same agentic task without
  explicit human review.
- When SEP #1561 (`unsafeOutputHint`) is finalised, add it to `logs_window`,
  `packets_window`, and the `ble_sniff_*` capture readers.

## Tools with elevated risk

`esptool_raw`, `nrfutil_raw`, `picotool_raw` accept arbitrary argument lists passed
directly to hardware flashing binaries. The confirmation gate covers destructive
subcommands (write-flash, dfu, load, erase) wherever they sit in the list, but all arguments should come from a
trusted source. Do not allow untrusted content (e.g. from mesh packets) to flow
into argument lists.

`recorder_export` writes to an arbitrary `dest_dir` on the MCP server's host
filesystem. Ensure the path is within an expected directory.

`vanity_grind_start` / `vanity_grind_poll` produce and return **private-key
material**. Hits are written to `<MCP data dir>/grinds/<job_id>.keys` and to the
job log (mvgrind prints them to stdout), both mode `0600`, and are returned
inline so `vanity_apply` can consume them — so they also pass through the model's
context. Treat the transcript and those files as secrets.

`vanity_apply` replaces a device's identity: the NodeNum, the keypair, and the
colour every app paints it. The old NodeNum is dropped from the node's own DB and
peers must re-learn the new key. It is `confirm`-gated and `destructiveHint`;
keep the previous private key if you want a way back. See `docs/vanity.md`.
