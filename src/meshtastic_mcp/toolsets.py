# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Trim the advertised tool list to the groups a session needs.

Every tool schema a client loads costs context. `MESHTASTIC_MCP_TOOLSETS` names the groups to
expose. Unset or `all` exposes every group but the opt-in `legacy`, which a client gets by
naming it; a `-group` entry drops one.
`MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state. A hidden tool is not
listed and cannot be called.
"""

from __future__ import annotations

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
            "build_poll",
            "build_start",
            "clean",
            "erase_and_flash",
            "flash_poll",
            "flash_start",
            "get_board",
            "list_boards",
            "pio_flash",
            "push_fake_nodedb",
            "update_flash",
            "userprefs_get",
            "userprefs_manifest",
            "userprefs_reset",
            "userprefs_set",
            "userprefs_testing_profile",
        }
    ),
    "hardware": frozenset(
        {
            "capture_screen",
            "esptool_chip_info",
            "esptool_erase_flash",
            "esptool_raw",
            "install",
            "nrfutil_dfu",
            "nrfutil_raw",
            "picotool_info",
            "picotool_load",
            "picotool_raw",
            "touch_1200bps",
            "uhubctl_cycle",
            "uhubctl_list",
            "uhubctl_power",
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
            "device_info",
            "get_channel_url",
            "get_config",
            "list_nodes",
            "set_config",
            "set_debug_log_api",
            "set_owner",
        }
    ),
    "local-model": frozenset(
        {
            "local_model_serve",
            "local_model_serve_stop",
            "local_model_status",
            "summarize_window",
            "triage_window",
            "vision_oracle",
        }
    ),
}
_PREFIXES: dict[str, tuple[str, ...]] = {
    "android": ("android_", "atak_"),
    "apple": ("ios_", "screenshot_set_"),
    "discord": ("discord_",),
    "hardware": ("nrf52_", "serial_"),
    "replay": ("replay_", "cot_relay_"),
    "rf": ("ble_sniff_", "vanity_", "pa_", "rf_"),
    "sdk": ("sdk_",),
}

# Groups the default leaves out.
OPT_IN = frozenset({"legacy"})


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
    if value is None or not value.strip() or value.strip().lower() == "all":
        return default_toolsets()
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    dropped = frozenset(n[1:] for n in names if n.startswith("-"))
    named = frozenset(n for n in names if not n.startswith("-"))
    unknown = (named - {"all"} | dropped) - known_toolsets()
    if unknown:
        raise ToolsetError(
            f"unknown toolset(s) {sorted(unknown)}; choose from {sorted(known_toolsets())} or 'all'"
        )
    if named and "all" not in named:
        base = named
    else:
        base = default_toolsets() | (named - {"all"})
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
