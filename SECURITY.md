# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories
(<https://github.com/meshtastic/meshtastic-mcp/security/advisories/new>) rather than a public
issue. We aim to acknowledge within a few days.

## Scope notes

- Destructive device operations (`reboot`, `shutdown`, `factory_reset`, `erase_and_flash`,
  `uhubctl_*`) are `confirm`-gated and `destructiveHint`-annotated. Treat any path that
  bypasses the gate as a security-relevant bug.
- Never log or transmit PII, location, or cryptographic keys.

## Prompt injection / lethal trifecta

The [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) for AI
agents is: **private data + untrusted content + exfiltration**. All three legs are present
in a full meshtastic-mcp session:

| Leg | Tools |
|---|---|
| Private data | `device_info`, `list_nodes`, `get_config`, `get_channel_url` |
| Untrusted content | `logs_window`, `packets_window` — return user-authored payloads from remote mesh nodes |
| Exfiltration | `send_text` — broadcasts a mesh message |

A hostile node on the same mesh could embed instructions in a packet payload. If an agent
processes that payload alongside `device_info` and has `send_text` available, an attacker
could exfiltrate the device's channel URL or node list.

**Mitigations in place:**
- `logs_window` and `packets_window` are classified `openWorldHint: true` so clients can
  detect that untrusted content has entered the session.
- `send_text` is `destructiveHint: true` — clients should prompt before broadcasting.
- The `confirm=True` gate on destructive ops adds a human-in-the-loop layer.

**Recommended operational posture:**
- Do not process untrusted mesh content and send text in the same agentic task without
  explicit human review.
- When SEP #1561 (`unsafeOutputHint`) is finalised, add it to `logs_window` and
  `packets_window`.

## Tools with elevated risk

`esptool_raw`, `nrfutil_raw`, `picotool_raw` accept arbitrary argument lists passed
directly to hardware flashing binaries. The `confirm=True` gate blocks destructive
subcommands (write-flash, dfu, load, erase), but all arguments should come from a
trusted source. Do not allow untrusted content (e.g. from mesh packets) to flow
into argument lists.

`recorder_export` writes to an arbitrary `dest_dir` on the MCP server's host
filesystem. Ensure the path is within an expected directory.
