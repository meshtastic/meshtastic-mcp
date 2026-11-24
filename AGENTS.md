# AGENTS.md: meshtastic-mcp

Guidance for agents changing this repo. `CLAUDE.md` and `GEMINI.md` point here. User docs are in [docs/](docs/), with the index in [llms.txt](llms.txt).

## What this is

An MCP server and bundled skills for discovering, driving, observing and testing Meshtastic radios and the apps that talk to them. The core runs with no firmware checkout: serial, TCP and BLE radios, the recorder, the simulated radio and the nRF52 UF2 bootloader. Firmware builds, the app drivers, RF instruments and the rest register only when their prerequisite is present.

## Architecture

- **Tools** live in `tools/`, one module per group. `tools/_app.py` holds the `FastMCP` app, `CAPS` and the capability decorators; `tools/_shared.py` the helpers several modules share. `server.py` imports every tool module, adds the transforms, starts the recorder and registers resources and prompts.
- **Metadata at the tool.** `@meta(kind, open_world=, idempotent=, title=, port=)` from `tool_meta.py` sits directly above each tool's `def`: `kind` is `read`, `write` or `destructive`, and `port=` says whether the `port` argument is a device. `_AnnotationTransform` turns it into MCP hints and `_LeaseTransform` into lease checks. A test fails for a tool without it.
- **One device path.** Every radio read and write goes through `backends/`: `read`, `configure`, `send` and `admin` on the `python` backend (meshtastic-python, in process) or the `sdk` backend (the Kotlin SDK CLI's `mcp` mode, with `MESHTASTIC_MCP_BACKEND=sdk`). Backends return plain dicts; `protodict` includes default values and withholds fields the protos mark `sensitive`. `backends/legacy.py` gives the pre-1.0 result shapes over the same calls; `backends/protopath.py` resolves config dot-paths.
- **Toolsets.** `toolsets.py` hides groups a session does not want. `core`, `app`, `hardware` and `replay` are on by default; the specialist groups come with `all`; `legacy`, the pre-1.0 names, only when named. `retired_tools.py` answers a call to a hidden or removed name with the call that replaces it. See [docs/toolsets.md](docs/toolsets.md).
- **Capabilities.** `capabilities.detect()` runs once at startup and decides which gated tools register. `config.firmware_root()` raises when unset; use `config.firmware_root_or_none()` in checks. Tools that report missing hardware (`pa_meter_status`, `ble_sniff_status`, `vanity_preview`, `vanity_apply`) always register.
- **Bench plumbing.** `serve.py` is HTTP with a bearer token; `bridge.py` makes a plain stdio start join this host's running `serve`. `leases.py` keeps leases as files in the data dir, keyed by USB VID:serial or TCP endpoint, so every server on the host sees them. `device_names.py` names a radio `<model>-<last four hex>` on first connect and never renames it. `call_log.py` logs one line per tool call. See [docs/bench.md](docs/bench.md).
- **Long work.** `jobs.py` is the one job registry. `tasks.py` wraps a job as an MCP background task when the `[tasks]` extra is installed; it never keeps a registry of its own.
- **Confirmation.** `confirm.py` is the gate every destructive tool calls. See [docs/safety.md](docs/safety.md).
- **Provisioning.** `doctor.py` checks every dependency and prints the install command for this host. It is the one place install hints live.
- **Docs generator.** `docs_gen.py` writes `docs/tools.md`, `docs/configuration.md`, the toolset tables in `README.md` and `docs/toolsets.md`, and `llms.txt`. `env_vars.py` lists every environment variable the code reads.
- **Outside the MCP server.** `discord_server.py` is the separate `meshtastic-mcp-discord` server. `web/` and `web-ui/` are FleetSuite, the bench web control plane (`[web]` extra). `cli/` holds standalone CLIs.

## Adding or changing a tool

1. Put it in its group's module with `@meta(...)` above the `def`. A new module needs an import in `server.py`.
2. Give it a group: a name or prefix in `toolsets._GROUPS` or `_PREFIXES`. A tool in no group is `core`, and `tests/unit/test_toolsets.py` pins `core` to an allowlist.
3. Write the docstring as a description and an `Args:` section. fastmcp drops `Returns:`, `Example:` and anything after a section, so name the result fields in one description line (``Returns `{ok, packet_id}`.``). `tests/unit/test_tool_docs.py` checks every tool.
4. Return a TypedDict from `schemas.py`. A key only some paths return is `NotRequired`; a field whose type varies is `Any`. `tests/unit/test_output_schemas.py` runs each path through a validating client.
5. Raise `fastmcp.exceptions.ToolError` on failure, with the next step in the message ("power-cycle it (uhubctl_cycle) and retry"). Never return `{"error": ...}`.
6. Reach the radio through a backend method, never meshtastic-python directly. A meshtastic-python exit inside a connection becomes a tool error; keep it that way.
7. Run `meshtastic-mcp docs generate` and commit what it writes.

## Rules

- **Python 3.11+, light core.** The core depends on `fastmcp`, `mcp`, `pyserial`, `meshtastic`, `platformdirs`, `duckdb` and `pytz`. Heavier dependencies go in extras and are imported behind a guard, as `tasks.py` does. A git-only dependency goes in a `[dependency-groups]` group, never an extra, because PyPI rejects URL dependencies.
- **The protos are ours.** `meshtastic_mcp.protos` serves every `meshtastic.protobuf.*_pb2` import from `protos/generated/` at the commit in `PROTOBUFS_REF`, installed when `meshtastic_mcp` is imported, so nothing may import `meshtastic` first. Bump with `scripts/regen_protos.sh <ref>`.
- **No firmware tree in core.** Core modules import and run without `MESHTASTIC_FIRMWARE_ROOT`. The data dir is `MESHTASTIC_MCP_DATA_DIR`, then platformdirs, never firmware-relative.
- **One firmware tree, shared.** Every session on a host builds in the same `.pio` tree, and PlatformIO cleans another env's build when the env changes. Flash a finished build at once; check for other builders before a multi-device session.
- **One call per serial port.** The port lock does not queue: a second call fails fast with a "busy" error and the caller retries.
- **Jobs, not long timeouts.** Anything that can outrun a 60 second MCP call runs as a job through `jobs.run_sync`, which hands the `job_id` to the client before it waits. A flash keeps running when its call is canceled; only builds started `cancellable=True` stop.
- **Async tools stay off the event loop.** A gated tool is `async` so it can ask the human; everything after the gate runs in `_off_loop`.
- **Destructive tools stay gated.** A destructive tool calls `confirm.gate(ctx, ...)` before taking any lock or starting any job, because the call reruns from the top once the human answers. Gate irreversible work with `irreversible=True`: it then asks a person even when an agent passes `confirm=True`, unless the server runs with `MESHTASTIC_MCP_CONFIRM=agent`. Never bypass the gate.
- **Untrusted content.** Mesh packets and logs, app screens and logcat, CoT callsigns, Discord messages and BLE device names are attacker-controlled text. Never let an agent read them and `send` in the same task without a human review. See `SECURITY.md`.
- **No type debt.** mypy runs with no per-module ignores, and ruff has no blanket `# noqa`.
- **Tests never reach the bench.** Mock every subprocess that could run `pio run -t upload`: an invalid upload port makes PlatformIO auto-detect and flash whatever board answers.
- **License and commits.** GPL-3.0-only, DCO sign-off (`git commit -s`), Conventional titles, no co-author trailers.
- **Releases** follow `CONTRIBUTING.md`: a CHANGELOG PR, then a tag on its merge commit, which publishes to PyPI, GitHub releases and the MCP Registry.

## Commands

```bash
uv sync --extra test --extra dev           # dev environment
uv run python -m meshtastic_mcp            # the server over stdio
uv run meshtastic-mcp install --local      # register this checkout with the MCP client
uv run meshtastic-mcp doctor               # what this host can do, and how to fix the rest
uv run meshtastic-mcp devices              # attached radios, no MCP client needed
uv run meshtastic-mcp docs generate        # regenerate the docs after a tool or env var change
```

## Gates

Run before every push; CI runs the same. ruff also formats the Python blocks in Markdown, so run `ruff format .` after editing docs.

```bash
uv run ruff check . && uv run ruff format --check .
uv run --extra dev mypy
python3 scripts/check_spdx.py
uv run meshtastic-mcp docs generate --check
uv run --extra test python -m pytest tests/unit -q
```

`MESHTASTIC_FIRMWARE_ROOT=<firmware> uv run --extra test python -m pytest tests/unit` adds the firmware tier. The hardware suite in `tests/` runs against a real bench; see [tests/README.md](tests/README.md).

## Where things are documented

[docs/tools.md](docs/tools.md) for every tool, [docs/configuration.md](docs/configuration.md) for every environment variable, [docs/troubleshooting.md](docs/troubleshooting.md) for failures, [docs/firmware-builds.md](docs/firmware-builds.md) for builds, flashing and nRF52 recovery, [docs/e2e.md](docs/e2e.md) for app testing, and one page per instrument (`rf-oracle`, `power-meter`, `ble-sniffer`, `vanity`, `display-mirror`, `atak-cot`). Procedures agents follow are the skills under `src/meshtastic_mcp/skills/`.
