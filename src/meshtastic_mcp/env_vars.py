# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Every environment variable the package reads, with what it does.

`docs/configuration.md` is generated from this table, and `tests/unit/test_env_vars.py`
fails when the code reads a variable this table does not name, or names one the code no
longer reads.
"""

from __future__ import annotations

from typing import NamedTuple


class EnvVar(NamedTuple):
    name: str
    section: str
    description: str


SECTIONS = (
    "Server",
    "Shared bench",
    "Source trees",
    "Tool binaries",
    "Apps",
    "Camera and OCR",
    "Discord server",
    "Web control plane",
    "Tests and the test TUI",
)

ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar(
        "MESHTASTIC_MCP_TOOLSETS",
        "Server",
        "Comma-separated tool groups to expose. Unset exposes the default groups; `all` adds"
        " every group but `legacy`, which comes only when named; `-group` drops one. See"
        " [toolsets.md](toolsets.md).",
    ),
    EnvVar(
        "MESHTASTIC_MCP_READ_ONLY",
        "Server",
        "`1` hides every tool that can change state.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_CONFIRM",
        "Server",
        "`human` makes irreversible tools ask the human even when the agent passes"
        " `confirm=True`. See [safety.md](safety.md).",
    ),
    EnvVar(
        "MESHTASTIC_MCP_BACKEND",
        "Server",
        "`sdk` runs device tools through the Kotlin SDK CLI instead of meshtastic-python."
        " `ble:` targets use meshtastic-python unless this is set.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_SDK_CLI",
        "Server",
        "Path to the SDK `cli` launcher the SDK backend runs.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_DATA_DIR",
        "Server",
        "Where leases, device names, snapshots and job state live. Defaults to the platform's"
        " user data dir.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_LOG_DIR",
        "Server",
        "Where the recorder writes its streams. Defaults to a directory under the data dir.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_CAPTURE_DIR",
        "Server",
        "Base directory for files tools write (screenshots, captures). Defaults to the"
        " working directory.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_FLASH_LOG",
        "Server",
        "A file each flash and tool subprocess tees its output to as it runs.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_TCP_HOST",
        "Server",
        "A `meshtasticd` TCP endpoint (`host` or `host:port`) listed first in `devices`.",
    ),
    EnvVar(
        "MESHTASTIC_REPLAY_OBSERVER_NUM",
        "Server",
        "The node number the simulated radio reports as its own.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_STANDALONE",
        "Shared bench",
        "`1` makes the stdio server run on its own instead of bridging to this host's"
        " running `serve`. See [bench.md](bench.md).",
    ),
    EnvVar(
        "MESHTASTIC_MCP_TOKEN",
        "Shared bench",
        "The bearer token `serve` requires, at least 32 characters.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_TOKEN_FILE",
        "Shared bench",
        "A file holding the `serve` token, read when `MESHTASTIC_MCP_TOKEN` is unset.",
    ),
    EnvVar(
        "MESHTASTIC_WORKSPACE",
        "Shared bench",
        "A workspace holding a `meshtastic-mcp` checkout; `doctor` compares the running"
        " install and `serve` against it.",
    ),
    EnvVar(
        "MESHTASTIC_FIRMWARE_ROOT",
        "Source trees",
        "A firmware checkout. Enables the `firmware` group and source builds in `install`.",
    ),
    EnvVar(
        "MESHTASTIC_ANDROID_ROOT",
        "Source trees",
        "A Meshtastic-Android checkout, for the Android app tools.",
    ),
    EnvVar(
        "MESHTASTIC_APPLE_ROOT",
        "Source trees",
        "A Meshtastic-Apple checkout, for the iOS app tools.",
    ),
    EnvVar(
        "MESHTASTIC_SDK_ROOT",
        "Source trees",
        "A meshtastic-sdk checkout; the SDK CLI is found under its build output.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_SOURCE_ROOT",
        "Source trees",
        "This package's own checkout, for the nightly bake. Defaults to the installed"
        " source's repository.",
    ),
    EnvVar("MESHTASTIC_PIO_BIN", "Tool binaries", "PlatformIO `pio` to use."),
    EnvVar("MESHTASTIC_ESPTOOL_BIN", "Tool binaries", "`esptool` to use."),
    EnvVar("MESHTASTIC_NRFUTIL_BIN", "Tool binaries", "`nrfutil` or `adafruit-nrfutil` to use."),
    EnvVar("MESHTASTIC_PICOTOOL_BIN", "Tool binaries", "`picotool` to use."),
    EnvVar("MESHTASTIC_UHUBCTL_BIN", "Tool binaries", "`uhubctl` to use."),
    EnvVar("MESHTASTIC_VHFILTER_BIN", "Tool binaries", "`vhfilter` to use."),
    EnvVar("MESHTASTIC_MCP_MVGRIND", "Tool binaries", "`mvgrind` to use for vanity keys."),
    EnvVar(
        "MESHTASTIC_MCP_BLE_SNIFFER",
        "Tool binaries",
        "Nordic's `nrfutil-ble-sniffer` plugin binary.",
    ),
    EnvVar(
        "ANDROID_HOME",
        "Apps",
        "The Android SDK, for app builds. `ANDROID_SDK_ROOT` is read when this is unset.",
    ),
    EnvVar("ANDROID_SDK_ROOT", "Apps", "The Android SDK, read when `ANDROID_HOME` is unset."),
    EnvVar(
        "MESHTASTIC_MCP_DESKTOP_URL",
        "Apps",
        "The desktop app's MCP endpoint, which `app` uses for desktop actions.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_PORT",
        "Apps",
        "The desktop app's MCP port, used when `MESHTASTIC_MCP_DESKTOP_URL` is unset.",
    ),
    EnvVar(
        "MESHTASTIC_UI_CAMERA_BACKEND",
        "Camera and OCR",
        "The webcam capture backend for `capture_screen`.",
    ),
    EnvVar(
        "MESHTASTIC_UI_CAMERA_DEVICE",
        "Camera and OCR",
        "The webcam device `capture_screen` opens.",
    ),
    EnvVar("MESHTASTIC_UI_OCR_BACKEND", "Camera and OCR", "The OCR backend for screen reads."),
    EnvVar(
        "DISCORD_BOT_TOKEN",
        "Discord server",
        "A read-only bot token for `meshtastic-mcp-discord`. See [discord.md](discord.md).",
    ),
    EnvVar("DISCORD_GUILD_ID", "Discord server", "The Discord server to read."),
    EnvVar(
        "DISCORD_USER",
        "Discord server",
        "Your Discord user, which `author` and `mentions` accept as `me`.",
    ),
    EnvVar(
        "DISCORD_TRUST_TIERS",
        "Discord server",
        "Overrides the role-to-trust-tier map on returned messages.",
    ),
    EnvVar("MESHTASTIC_MCP_WEB_DB", "Web control plane", "The FleetSuite registry database."),
    EnvVar(
        "MESHTASTIC_MCP_ARTIFACT_DIR",
        "Web control plane",
        "Where FleetSuite keeps build artifacts.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_AUTO_ENRICH",
        "Web control plane",
        "`0` stops FleetSuite reading device details on discovery.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_AUTO_UNWEDGE",
        "Web control plane",
        "`0` stops FleetSuite power-cycling a wedged board's hub port.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_NIGHTLY_FW_DIR",
        "Web control plane",
        "The firmware checkout the nightly bake owns and may reset.",
    ),
    EnvVar(
        "MESHTASTIC_MCP_RECORDER_DIR",
        "Web control plane",
        "The recorder directory FleetSuite ships to Datadog.",
    ),
    EnvVar("MESHTASTIC_MCP_DD_SITE", "Web control plane", "The Datadog site to ship to."),
    EnvVar(
        "MESHTASTIC_MCP_SEED",
        "Tests and the test TUI",
        "The seed the test suite derives its channel PSK from.",
    ),
    EnvVar(
        "MESHTASTIC_UI_TUI_CAMERA",
        "Tests and the test TUI",
        "`1` shows the webcam in the test TUI.",
    ),
)
