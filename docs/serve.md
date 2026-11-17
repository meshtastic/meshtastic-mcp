# Serving a shared bench over HTTP

`meshtastic-mcp serve` runs the MCP server over streamable HTTP on the machine the radios are plugged into. One process then owns the serial ports, the recorder and the firmware tree, so every client shares one arbiter instead of each spawning its own stdio child.

```bash
export MESHTASTIC_MCP_TOKEN_FILE=~/.config/meshtastic-mcp/token   # 0600, 32+ characters
meshtastic-mcp serve --host 0.0.0.0 --port 8765
```

Every request needs `Authorization: Bearer <token>`. There is no anonymous mode: the server refuses to start without a token, rejects one shorter than 32 characters, and rejects a token file that group or other can read. Generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.

The token is a shared secret over plain HTTP, so keep the bind address on a trusted LAN or a VPN interface. The tools can flash, erase and factory-reset boards; the `confirm` gates still apply.

## Confirmations over HTTP

Each HTTP client declares its own capabilities, so a client that says it supports elicitation gets the confirmation prompt wherever it chooses to show it. Nothing proves a person answered, and `MESHTASTIC_MCP_CONFIRM=human` cannot tell either, so over HTTP the prompt is not a human gate. Anyone holding the token can run every tool the server exposes. See [confirmations.md](confirmations.md).

`MESHTASTIC_MCP_TOOLSETS` and `MESHTASTIC_MCP_READ_ONLY` are read by the server process, so they shape the tool list for every client; see [toolsets.md](toolsets.md).

## Pointing a client at it

```bash
claude mcp add --transport http meshtastic http://<bench-host>:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/meshtastic-mcp/token)"
```

## One owner per bench

The per-port lock and the job registry live inside one process. A stdio client started alongside the HTTP server opens its own copy of both and can race it for a port, so point every client on the bench at the server. Every client then shares one job registry, so a `job_id` one client started can be polled from another; see [background-jobs.md](background-jobs.md).
