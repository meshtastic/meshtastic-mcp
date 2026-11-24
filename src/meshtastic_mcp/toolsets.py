# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Trim the advertised tool list to the groups a session needs.

Every tool schema a client loads costs context. `MESHTASTIC_MCP_TOOLSETS` names the groups to
expose. Unset exposes the device, app, hardware and replay groups; `all` adds the specialist
groups (firmware, android, apple, rf, sdk, display, tak), and `legacy`, the pre-1.0 names,
comes only when named. A `-group` entry drops one.
`MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state. A hidden tool is not
listed and cannot be called.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

TOOLSETS_ENV = "MESHTASTIC_MCP_TOOLSETS"
READ_ONLY_ENV = "MESHTASTIC_MCP_READ_ONLY"

CORE = "core"

# Tools outside `core`, by group. A tool in no group is core, and
# tests/unit/test_toolsets.py pins that set to an allowlist.
_GROUPS: dict[str, frozenset[str]] = {
    "firmware": frozenset(
        {
            "build",
            "clean",
            "get_board",
            "list_boards",
            "push_fake_nodedb",
            "userprefs_get",
            "userprefs_manifest",
            "userprefs_reset",
            "userprefs_set",
            "userprefs_testing_profile",
        }
    ),
    # One entry point over the android, apple and desktop app drivers. A group of its own, so a
    # session can take it without the emulator and simulator tools, or drop it with `-app`.
    "app": frozenset({"app", "app_view"}),
    "hardware": frozenset(
        {
            "install",
            "touch_1200bps",
            "uhubctl_cycle",
            "uhubctl_list",
            "uhubctl_power",
            "vendor",
        }
    ),
    "rf": frozenset(
        {
            "ble_sniff_status",
            "pa_measure",
            "pa_meter_status",
            "pa_sweep",
            "vanity_apply",
            "vanity_preview",
        }
    ),
    # The pre-1.0 names a `next` tool replaces. Opt-in: only a client that names it gets them.
    "legacy": frozenset(
        {
            "list_devices",
            "events_window",
            "logs_window",
            "packets_window",
            "telemetry_timeline",
            "triage_bundle",
            "drive_display_poll",
            "android_tap",
            "android_swipe",
            "android_type_text",
            "android_clear_logcat",
            "android_screenshot",
            "android_ui_dump",
            "android_find_text",
            "android_poll_for_text",
            "android_read_logcat",
            "android_resolve",
            "ios_boot",
            "ios_open_url",
            "ios_tap",
            "ios_type_text",
            "ios_ui_dump",
            "screenshot_set_poll",
            "build_start",
            "build_poll",
            "flash_start",
            "flash_poll",
            "pio_flash",
            "erase_and_flash",
            "update_flash",
            "esptool_chip_info",
            "esptool_erase_flash",
            "nrfutil_dfu",
            "picotool_info",
            "picotool_load",
            "nrf52_flash_uf2",
            "serial_list",
            "serial_read",
            "ble_sniff_poll",
            "vanity_grind_poll",
            "sdk_device_info",
            "sdk_list_nodes",
            "sdk_send_text",
            "sdk_status",
            "replay_fuzz_presets",
            "replay_start",
            "replay_status",
            "replay_stop",
            "device_info",
            "esptool_raw",
            "factory_reset",
            "get_channel_url",
            "get_config",
            "list_nodes",
            "nrfutil_raw",
            "picotool_raw",
            "reboot",
            "replay_inject",
            "replay_inject_beacon",
            "replay_inject_client_notification",
            "replay_inject_fileinfo",
            "replay_inject_traceroute",
            "replay_inject_waypoint",
            "set_config",
            "set_debug_log_api",
            "set_owner",
            "shutdown",
            "send_text",
        }
    ),
    # The device's own screen: mirror, input and the webcam.
    "display": frozenset(
        {"capture_display", "capture_screen", "drive_display_start", "send_input_event"}
    ),
    # `sim` and `sim_status` match no `replay_` prefix.
    "replay": frozenset({"inject", "sim", "sim_status"}),
}
_PREFIXES: dict[str, tuple[str, ...]] = {
    "android": ("android_",),
    "tak": ("atak_", "cot_relay_"),
    "apple": ("ios_", "screenshot_set_"),
    "hardware": ("nrf52_", "serial_"),
    "replay": ("replay_",),
    "rf": ("ble_sniff_", "vanity_", "pa_", "rf_"),
    "sdk": ("sdk_",),
}

# Groups the default leaves out. `all` adds every one but `legacy`, which only naming it adds.
LEGACY = "legacy"
OPT_IN = frozenset({LEGACY, "firmware", "android", "apple", "rf", "sdk", "display", "tak"})


log = logging.getLogger(__name__)

# What each group is for, in the generated table in docs/toolsets.md.
SUMMARIES: dict[str, str] = {
    CORE: "The device intents, leases, long work, the recorder and its queries, config"
    " snapshots, frame injection and `doctor`. Needs only the base install and a radio.",
    "app": "One entry point over the Android, iOS and desktop app drivers. Desktop actions"
    " need `MESHTASTIC_MCP_DESKTOP_URL`.",
    "hardware": "Flashing (`install`), vendor tools (`vendor`), the nRF52 UF2 bootloader,"
    " raw serial sessions that feed the recorder, and hub power.",
    "replay": "The simulated radio and frame injection into it.",
    "firmware": "Builds, boards and userPrefs from a firmware tree. Registered only with"
    " `MESHTASTIC_FIRMWARE_ROOT`; `install` builds without this group.",
    "display": "The device's own screen, and the webcam (the `[ui]` extra).",
    "android": "Android tools `app` does not cover.",
    "apple": "iOS tools `app` does not cover, and matching Android and iOS screenshot sets.",
    "tak": "The ATAK emulator fleet and the CoT relay.",
    "rf": "The SDR oracle, the power meter, the BLE sniffer and vanity keys.",
    "sdk": "Nothing of its own; the SDK backend runs under the device intents with"
    " `MESHTASTIC_MCP_BACKEND=sdk`.",
    LEGACY: "The pre-1.0 tools a newer tool replaces. A call to one by name while it is"
    " hidden returns the call that replaces it.",
}

# Groups 0.x advertised that 1.0 no longer has: a config that names one still starts.
RETIRED = {
    "discord": "the discord_* tools moved to the separate meshtastic-mcp-discord server",
    "local-model": "the local-model tools were removed",
}


class ToolsetError(ValueError):
    """An unknown toolset name was requested."""


def toolset_of(name: str) -> str:
    for group, names in _GROUPS.items():
        if name in names:
            return group
    for group, prefixes in _PREFIXES.items():
        if name.startswith(prefixes):
            return group
    return CORE


def known_toolsets() -> frozenset[str]:
    return frozenset({CORE, *_GROUPS, *_PREFIXES})


def default_toolsets() -> frozenset[str]:
    return known_toolsets() - OPT_IN


def parse_toolsets(value: str | None) -> frozenset[str]:
    """The groups to expose: every group but the opt-in ones, or the named groups.

    A `-group` entry removes that group; with no group named, it removes it from the default.
    """
    if value is None or not value.strip():
        return default_toolsets()
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    for name in sorted({n.lstrip("-") for n in names} & RETIRED.keys()):
        log.warning("MESHTASTIC_MCP_TOOLSETS names %r, which is gone: %s", name, RETIRED[name])
    dropped = frozenset(n[1:] for n in names if n.startswith("-")) - RETIRED.keys()
    asked = frozenset(n for n in names if not n.startswith("-"))
    named = asked - RETIRED.keys()
    unknown = (named - {"all"} | dropped) - known_toolsets()
    if unknown:
        raise ToolsetError(
            f"unknown toolset(s) {sorted(unknown)}; choose from {sorted(known_toolsets())} or 'all'"
        )
    if "all" in asked:
        base = (known_toolsets() - {LEGACY}) | (named - {"all"})
    elif asked:
        base = named
    else:
        base = default_toolsets()
    return base - dropped


def is_visible(
    name: str,
    toolsets: frozenset[str],
    read_only: bool,
    read_only_names: Collection[str],
) -> bool:
    if read_only and name not in read_only_names:
        return False
    return toolset_of(name) in toolsets


def read_only_requested(env: Mapping[str, str]) -> bool:
    return env.get(READ_ONLY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class ToolsetTransform(Transform):
    """Hide tools outside the requested groups from listing and from calls."""

    def __init__(
        self,
        env: Mapping[str, str],
        read_only_names: Callable[[], Collection[str]],
    ) -> None:
        self._toolsets = parse_toolsets(env.get(TOOLSETS_ENV))
        self._read_only = read_only_requested(env)
        self._read_only_names = read_only_names

    @property
    def active(self) -> bool:
        return self._toolsets != known_toolsets() or self._read_only

    def _visible(self, name: str) -> bool:
        return is_visible(name, self._toolsets, self._read_only, self._read_only_names())

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t for t in tools if self._visible(t.name)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        if not self._visible(name):
            return None
        return await call_next(name, version=version)
