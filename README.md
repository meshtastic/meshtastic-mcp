# meshtastic-mcp

<!-- mcp-name: io.github.meshtastic/meshtastic-mcp -->

[![CI](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/meshtastic/meshtastic-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/meshtastic-mcp.svg)](https://pypi.org/project/meshtastic-mcp/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent discover, configure,
observe, flash and test Meshtastic radios and the apps that talk to them. It reads and changes
device config, sends mesh traffic, watches live packets and logs, builds and flashes firmware,
drives the Android and Apple apps, and serves a simulated radio for hardware-free tests. A
bundled skill set covers closed-loop device and app end-to-end testing. Core tools work against
any serial, TCP or BLE radio with no firmware checkout.

## Quick start (60 seconds)

Plug in a radio, then install and register the server with Claude Code:

```bash
uv tool install 'meshtastic-mcp[ui]'
claude mcp add meshtastic -s user -- meshtastic-mcp
meshtastic-mcp doctor          # check what is available on this machine
```

Restart the client, then ask it to make three calls:

```text
devices()
inspect(node="heltec-v3-aa13", what=["lora", "nodes"])
configure(node="heltec-v3-aa13", changes={"lora.region": "US"}, dry_run=True)
```

`devices` lists the attached radios by name, and those names go in every `node` argument.
`inspect` reads a radio. `configure` previews the change with `dry_run=True`; without it, the
call writes the change and reads it back. Both calls and every other tool are in
[docs/tools.md](docs/tools.md).

## Install

Claude Code:

```bash
uv tool install 'meshtastic-mcp[ui]'
claude mcp add meshtastic -s user -- meshtastic-mcp
```

Any other MCP client takes a stdio server entry:

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

`meshtastic-mcp install --client claude-code|claude-desktop|cursor|windsurf` writes a stdio entry
into the client's config for you, and `--print` shows it without editing anything. Extras, the
other clients, updating, source checkouts and a shared bench over HTTP are in
[docs/install.md](docs/install.md).

## Tools

The tool list is generated from the registered tools. Each tool's arguments and result are in
[docs/tools.md](docs/tools.md).

<!-- BEGIN GENERATED TOOLSETS: `meshtastic-mcp docs generate` -->
<!-- END GENERATED TOOLSETS -->

Tools belong to toolsets, and a capability decides whether a tool registers at all. A hidden
tool is not listed and cannot be called. [docs/toolsets.md](docs/toolsets.md) has the groups and
the prerequisite for each capability.

The device tools are `devices`, `inspect`, `configure`, `send`, `admin`, `query`, `wait_for`,
`job`, `claim`, `release`, `install` and `vendor`. The pre-1.0 tools (`device_info`,
`get_config`, `set_config`, `send_text`, `reboot` and the rest) are an opt-in `legacy` toolset:
`MESHTASTIC_MCP_TOOLSETS=all,legacy`. It is removed in 1.1.

Beyond tools, the server exposes MCP resources (`meshtastic://doctor`,
`meshtastic://capabilities` and the templated `meshtastic://e2e/{loop}`) and prompts
(`triage_e2e_failure`, `bringup_device`, `inbound_loop`).

## Configuration

Set these in the environment of the server process, or in the client's `env` block.

| Variable | Effect |
| --- | --- |
| `MESHTASTIC_MCP_TOOLSETS` | Comma-separated toolsets to expose. Unset or `all` is every group but `legacy`; `-group` drops one. |
| `MESHTASTIC_MCP_READ_ONLY` | `1` hides every tool that can change state. |
| `MESHTASTIC_MCP_CONFIRM` | `agent` (default) or `human`. `human` makes irreversible tools ask a person even when the agent passes `confirm=True`. |
| `MESHTASTIC_FIRMWARE_ROOT` | A firmware checkout. Registers the `firmware` toolset (build, flash, boards, userPrefs). |
| `MESHTASTIC_MCP_DATA_DIR` | Where the server keeps leases, device names, jobs, logs and snapshots. Defaults to the platform user-data directory. |
| `MESHTASTIC_MCP_BACKEND` | `python` or `sdk`. A `ble:` node always uses the SDK backend. |
| `MESHTASTIC_MCP_STANDALONE` | `1` makes a stdio client run its own server instead of bridging to a running `serve`. |

`MESHTASTIC_ANDROID_ROOT` and `MESHTASTIC_APPLE_ROOT` point the Android and Apple tools at
source checkouts. `meshtastic-mcp doctor` reports which prerequisites are present and prints the
command to fix each missing one.

## Security

- Tools that reboot, erase, reflash or rewrite a board take `confirm`. A client that can show a
  prompt asks the human; one that cannot gets a refusal naming `confirm=True`.
- `MESHTASTIC_MCP_CONFIRM=human` makes the irreversible tools ask a person even with
  `confirm=True`. Over HTTP the prompt is not a human gate, so the bearer token is the boundary.
- `claim` leases a radio so other sessions cannot use it, and `MESHTASTIC_MCP_READ_ONLY=1` removes
  every write tool.
- Mesh text, app UI, logcat and Discord messages are untrusted input that can carry
  instructions aimed at the agent.

The full model is in [docs/safety.md](docs/safety.md). Report a vulnerability as
[SECURITY.md](SECURITY.md) describes.

## Documentation

Start here:

- [docs/install.md](docs/install.md): every client, extras, updating, source checkouts
- [docs/tools.md](docs/tools.md): the generated tool reference
- [docs/toolsets.md](docs/toolsets.md): toolsets, capabilities and trimming the tool list
- [docs/safety.md](docs/safety.md): confirmations, leases, read-only mode, jobs, prompt injection
- [docs/troubleshooting.md](docs/troubleshooting.md): symptom, cause, fix
- [docs/serve.md](docs/serve.md): one shared server for a bench

Firmware and hardware:

- [docs/firmware-builds.md](docs/firmware-builds.md): build, flash, userPrefs, nRF52 UF2 flashing and recovery
- [docs/bench-setup.md](docs/bench-setup.md): your own hardware bench, FleetSuite and the hardware test suite
- [docs/power-meter.md](docs/power-meter.md), [docs/rf-oracle.md](docs/rf-oracle.md), [docs/ble-sniffer.md](docs/ble-sniffer.md), [docs/vanity.md](docs/vanity.md)
- [docs/display-mirror.md](docs/display-mirror.md), [docs/screenshot-set.md](docs/screenshot-set.md)

End-to-end testing:

- [docs/replay.md](docs/replay.md): a simulated radio, the synthetic mesh, the fuzzer and live injection
- [docs/atak-cot.md](docs/atak-cot.md), [docs/atak-cot-emission.md](docs/atak-cot-emission.md)
- [docs/sdk-cli-bridge.md](docs/sdk-cli-bridge.md), [docs/nightly.md](docs/nightly.md)

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Release history is in
[CHANGELOG.md](CHANGELOG.md).

## License

GPL-3.0-only.
