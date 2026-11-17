# Serving a shared bench over HTTP

`meshtastic-mcp serve` runs the MCP server over streamable HTTP on the machine the radios are plugged into. One process then owns the serial ports, the recorder and the firmware tree, so every client shares one arbiter instead of each spawning its own stdio child.

```bash
export MESHTASTIC_MCP_TOKEN_FILE=~/.config/meshtastic-mcp/token   # 0600, 32+ characters
meshtastic-mcp serve --host 0.0.0.0 --port 8765
```

Every request needs `Authorization: Bearer <token>`. There is no anonymous mode: the server refuses to start without a token, rejects one shorter than 32 characters, and rejects a token file that group or other can read. Generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.

The token is a shared secret over plain HTTP, so keep the bind address on a trusted LAN or a VPN interface. The tools can flash, erase and factory-reset boards; the `confirm` gates still apply.

`--host` takes one address and the server listens only there. Bound to a LAN address, it does not answer on `localhost`, so a client on the same machine uses that address too.

## Running it as a service

On a Linux bench host, a systemd user unit keeps the server up across reboots. It needs the same environment a stdio client would have set, because the capability gates read it at startup: the firmware and Android roots, `ANDROID_HOME`, and a `PATH` that finds `pio` and `adb`. Save it as `~/.config/systemd/user/meshtastic-mcp.service`.

```ini
[Unit]
Description=meshtastic-mcp bench server
After=network-online.target
StartLimitIntervalSec=0

[Service]
Environment=MESHTASTIC_MCP_TOKEN_FILE=%h/.config/meshtastic-mcp/token
Environment=MESHTASTIC_FIRMWARE_ROOT=%h/src/firmware
Environment=ANDROID_HOME=%h/Android/Sdk
Environment=PATH=%h/.local/bin:%h/.platformio/penv/bin:%h/Android/Sdk/platform-tools:/usr/local/bin:/usr/bin:/bin
ExecStartPre=/bin/sh -c "for i in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:11434/ && exit 0; sleep 1; done; exit 0"
ExecStart=%h/.local/bin/meshtastic-mcp serve --host 192.168.1.10 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Install it with `systemctl --user daemon-reload && systemctl --user enable --now meshtastic-mcp`, and run `loginctl enable-linger $USER` once, or the unit stops when the last session logs out. The bind address may not exist yet at boot, so `RestartSec` and `StartLimitIntervalSec=0` keep it retrying instead of landing in the failed state.

Capabilities are detected once, at startup. A server that starts before a backend it depends on is up omits those tools until it restarts: started seconds after boot, ahead of Ollama, the server above served 126 tools instead of 132 (the six `local_model` tools). The `ExecStartPre` line waits for Ollama; drop it where there is none. Compare the served `tools/list` with a stdio server's after any change to the unit or the host.

## Confirmations over HTTP

Each HTTP client declares its own capabilities, so a client that says it supports elicitation gets the confirmation prompt wherever it chooses to show it. Nothing proves a person answered, and `MESHTASTIC_MCP_CONFIRM=human` cannot tell either, so over HTTP the prompt is not a human gate. Anyone holding the token can run every tool the server exposes. See [confirmations.md](confirmations.md).

`MESHTASTIC_MCP_TOOLSETS` and `MESHTASTIC_MCP_READ_ONLY` are read by the server process, so they shape the tool list for every client; see [toolsets.md](toolsets.md).

## Pointing a client at it

```bash
claude mcp add --transport http meshtastic http://<bench-host>:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/meshtastic-mcp/token)"
```

## A client that only speaks stdio

A client that launches servers as stdio children (a generated `.mcp.json`, a launcher script) can bridge to the bench with FastMCP's proxy. FastMCP 4 builds it with `create_proxy`; `FastMCP.as_proxy` no longer exists. Keep the banner off, since stdout carries the protocol.

```python
import os
import sys
import urllib.error
import urllib.request

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

url = "http://192.168.1.10:8765/mcp"
token = open(os.environ["MESHTASTIC_MCP_TOKEN_FILE"]).read().strip()
headers = {"Authorization": f"Bearer {token}"}
try:
    urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5).close()
except urllib.error.HTTPError:
    pass  # any HTTP answer means the bench is up
except OSError as e:
    sys.exit(f"bench does not answer: {e}")
create_proxy(StreamableHttpTransport(url, headers=headers), name="meshtastic").run(
    show_banner=False
)
```

Run it under `uv run --no-project --with 'fastmcp>=4,<5' python bridge.py`. Reading the token from a file at launch keeps it out of the client's config. The proxy does not contact the bench until its first request, so without the probe a bench that is down shows up as an empty tool list and `Unknown tool` errors. With it the bridge exits and the client reports the server as failed. Falling back to a local stdio server would race the bench for its ports.

The prompt for a `confirm`-gated tool reaches the client behind the bridge. A headless client such as `claude -p` answers with the usual `was not confirmed, so nothing ran` refusal. A raw JSON-RPC probe that cannot answer an elicitation holds the call until its stdin closes.

A client host that cannot open LAN sockets from its interpreter can still reach the bench through `ssh -N -L 18765:<bench-host>:8765 <ssh-host>` and a `http://127.0.0.1:18765/mcp` URL. macOS blocks LAN access per binary for processes started under some terminal hosts, while `/usr/bin/ssh` and loopback connections work.

## One owner per bench

The per-port lock and the job registry live inside one process. A stdio client started alongside the HTTP server opens its own copy of both and can race it for a port, so point every client on the bench at the server. Every client then shares one job registry, so a `job_id` one client started can be polled from another; see [background-jobs.md](background-jobs.md).
