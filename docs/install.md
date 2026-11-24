# Installing meshtastic-mcp

The server is the `meshtastic-mcp` command. A client starts it over stdio, or connects to a shared one over HTTP. It needs Python 3.11 or newer.

## Install the package

```bash
uv tool install 'meshtastic-mcp[ui]'    # camera and OCR support for the on-device screen tools
uv tool install meshtastic-mcp          # without the [ui] extra
```

| Extra | Adds |
| --- | --- |
| `[ui]` | Camera and OCR for `capture_screen` and the on-device UI tests |
| `[tasks]` | MCP background tasks, so `build` and the flash tools run as tasks (see [safety.md](safety.md)) |
| `[web]` | FleetSuite, the web control plane for a bench (`meshtastic-mcp-web`) |
| `[sdr]` | The RF compliance oracle (see [rf-oracle.md](rf-oracle.md)) |
| `[test]` | The pytest harness and its terminal UI |
| `[inject]` | Channel encryption for `inject_frame` |
| `[sim]` | Richer synthetic node names for the replay simulator |
| `[android-fast]` | A resident uiautomator2 server for faster Android UI driving |

Combine extras in one list: `uv tool install 'meshtastic-mcp[ui,tasks]'`.

```bash
# Unreleased main, straight from git
uv tool install 'meshtastic-mcp[ui] @ git+https://github.com/meshtastic/meshtastic-mcp'

# A local checkout, editable
uv tool install --editable '/path/to/meshtastic-mcp[ui]'
```

Update with `uv tool upgrade meshtastic-mcp`. A git install updates by running the install command again, and an editable install by pulling the checkout and reinstalling when the dependencies change. A running client keeps the old server until you restart it.

## Register with a client

`meshtastic-mcp install` edits the client's `mcpServers` JSON in place, keeps its other entries, and also copies the bundled skills to `~/.agents/skills/`.

```bash
meshtastic-mcp install                          # Claude Code, user scope
meshtastic-mcp install --client cursor          # claude-code | claude-desktop | cursor | windsurf
meshtastic-mcp install --scope project          # the project's own config file
meshtastic-mcp install --local                  # register this interpreter (python -m meshtastic_mcp)
meshtastic-mcp install --env MESHTASTIC_FIRMWARE_ROOT=~/firmware
meshtastic-mcp install --print                  # print the JSON entry, edit nothing
meshtastic-mcp install --dry-run                # show what would change, write nothing
meshtastic-mcp install --no-skills              # skip the skills
meshtastic-mcp uninstall                        # remove the entry (--purge-skills drops the skills)
```

Without `--local`, the entry runs `uvx meshtastic-mcp`, so `uvx` must be on the client's `PATH`. The per-client sections below register the installed `meshtastic-mcp` binary directly. `--config PATH` edits a specific file, and `--name` changes the server name (default `meshtastic`).

Install or remove only the skills with `meshtastic-mcp skills install` and `meshtastic-mcp skills uninstall` (both take `--dest DIR`).

## Claude Code

```bash
claude mcp add meshtastic -s user -- meshtastic-mcp
claude mcp add meshtastic -s user -e MESHTASTIC_FIRMWARE_ROOT=$HOME/firmware -- meshtastic-mcp
```

`-s project` writes `.mcp.json` in the current project instead. Restart Claude Code, then call `devices()`.

## Claude Desktop

Add the entry to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": []
    }
  }
}
```

A desktop app often starts with a shorter `PATH` than your shell. If it cannot find the command, put the absolute path from `which meshtastic-mcp` in `command`. `meshtastic-mcp install --client claude-desktop` writes the file for you.

## VS Code

Add the entry to `.vscode/mcp.json` in the workspace. VS Code uses a `servers` key, not `mcpServers`:

```json
{
  "servers": {
    "meshtastic": {
      "type": "stdio",
      "command": "meshtastic-mcp",
      "args": []
    }
  }
}
```

## Cursor

Cursor reads `~/.cursor/mcp.json` for every project, or `.cursor/mcp.json` inside one project:

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": []
    }
  }
}
```

`meshtastic-mcp install --client cursor` writes the user file, and `--scope project` the project file. Windsurf uses the same entry in `~/.codeium/windsurf/mcp_config.json` (`--client windsurf`).

## Any other stdio client

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": [],
      "env": {
        "MESHTASTIC_MCP_TOOLSETS": "core,firmware",
        "MESHTASTIC_FIRMWARE_ROOT": "/path/to/firmware"
      }
    }
  }
}
```

The server reads its environment at startup, so put the variables a client should set in `env`. The variables are in the README's configuration table and [toolsets.md](toolsets.md).

## A shared bench over HTTP

`meshtastic-mcp serve` runs one server on the machine the radios are plugged into, behind a bearer token. The setup is in [serve.md](serve.md). Point Claude Code at it:

```bash
claude mcp add --transport http meshtastic http://<bench-host>:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/meshtastic-mcp/token)"
```

On the bench host, a client registered as plain `meshtastic-mcp` finds the running server and bridges to it. From another host, a client that only speaks stdio can bridge explicitly:

```json
{
  "mcpServers": {
    "meshtastic": {
      "command": "meshtastic-mcp",
      "args": [
        "bridge",
        "--url", "http://192.168.1.10:8765/mcp",
        "--token-file", "/home/me/.config/meshtastic-mcp/token"
      ]
    }
  }
}
```

## Source checkouts and prerequisites

Set an environment variable per source tree to turn on the matching tools:

| Variable | Turns on |
| --- | --- |
| `MESHTASTIC_FIRMWARE_ROOT` | Build, flash, boards and userPrefs (also needs PlatformIO) |
| `MESHTASTIC_ANDROID_ROOT` | `build_android_apk.sh` and the Android app builds |
| `MESHTASTIC_APPLE_ROOT` | `build_apple.sh` and the iOS Simulator app builds |

`meshtastic-mcp provision` clones the firmware, Android and Apple repositories into `~/.local/share/meshtastic-mcp/repos` (`--dir DIR` changes it) and writes a sourceable `.env` for the ones it cloned. It skips any repository whose variable is already set. Add the exports to your shell profile and to the client's `env` block, because the server process must see them.

esptool needs no setup when PlatformIO is installed: it is found in the PlatformIO environment.

`meshtastic-mcp doctor` probes every binary (`pio`, `adb`, `xcrun`, `idb_companion`, `ffmpeg`, `uhubctl`, OCR) and every source root, reports `ok`, `missing` or `degraded`, and prints the install command for each gap. `doctor --json` returns the same with a `fix_commands` list, and the `doctor` tool does the same inside a session.

The build scripts under `scripts/` clone fresh when given no source directory, accept `--ref` to pin a git sha, tag or branch, and print a `<component>-sha=<sha>` line for provenance:

| Script | Builds | Source override |
| --- | --- | --- |
| `scripts/build_meshtasticd.sh` | The native `meshtasticd` daemon (`--env native` or `native-macos`) | `--firmware-dir` or `MESHTASTIC_FIRMWARE_ROOT` |
| `scripts/build_android_apk.sh` | The Meshtastic-Android APK (`--variant`) | `--source-dir` or `MESHTASTIC_ANDROID_ROOT` |
| `scripts/build_apple.sh` | The Meshtastic iOS Simulator `.app` (`--sim`) | `--source-dir` or `MESHTASTIC_APPLE_ROOT` |

## Bundled skills

Three skills ship in the package, and `meshtastic-mcp install` or `meshtastic-mcp skills install` copies them to `~/.agents/skills/`:

- `meshtastic-device-ops`: discover, connect, configure, observe, recover and flash through the tools.
- `meshtastic-e2e`: device and app end-to-end testing, with per-loop references, a device-plane helper, journey-driven UI and triage.
- `meshtastic-org-knowledge`: questions that span the Meshtastic GitHub organization.
