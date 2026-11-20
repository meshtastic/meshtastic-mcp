# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Toolset grouping and the transform that hides tools outside the requested groups."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest

from meshtastic_mcp import toolsets

# The portable device set: tools that need only the base install and a radio over serial or
# TCP. A new tool that matches no group lands here, so this list fails until it is placed.
_CORE = frozenset(
    {
        "capture_display",
        "claim",
        "config_diff",
        "config_snapshot",
        "config_snapshots_list",
        "configure",
        "devices",
        "doctor",
        "drive_display_poll",
        "drive_display_start",
        "events_window",
        "factory_reset",
        "inject_frame",
        "inspect",
        "job",
        "list_devices",
        "logs_window",
        "mark_event",
        "packets_window",
        "query",
        "reboot",
        "recorder_export",
        "release",
        "recorder_pause",
        "recorder_resume",
        "recorder_status",
        "send_input_event",
        "send_text",
        "set_channel_url",
        "shutdown",
        "telemetry_timeline",
        "triage_bundle",
        "wait_for",
    }
)


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _is_tool_decorator(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "tool" and isinstance(func.value, ast.Name) and func.value.id == "app"
    return isinstance(func, ast.Name) and func.id.endswith("_tool")


def _defined_tools() -> set[str]:
    """Every tool `server.py` and `tools/` define, whether or not this host registers it."""
    spec = importlib.util.find_spec("meshtastic_mcp.server")
    assert spec is not None and spec.origin is not None
    server_py = Path(spec.origin)
    sources = [server_py, *sorted((server_py.parent / "tools").glob("*.py"))]
    return {
        node.name
        for source in sources
        for node in ast.parse(source.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_tool_decorator(d) for d in node.decorator_list)
    }


def test_tool_discovery_sees_gated_and_registered_tools(server) -> None:
    defined = _defined_tools()
    assert {t.name for t in asyncio.run(server.app.list_tools())} <= defined
    assert {"build", "ios_boot", "rf_scan", "sdk_status", "screenshot_set_start"} <= defined


def test_core_is_exactly_the_portable_device_set() -> None:
    core = {name for name in _defined_tools() if toolsets.toolset_of(name) == toolsets.CORE}
    assert core - _CORE == set(), "tools that fell into core; give each a group"
    assert _CORE - core == set(), "allowlisted core tools that are gone or grouped"


def test_unset_and_all_mean_every_group_but_legacy() -> None:
    every = toolsets.known_toolsets() - {"legacy"}
    assert toolsets.parse_toolsets(None) == every
    assert toolsets.parse_toolsets("") == every
    assert toolsets.parse_toolsets("ALL") == every


def test_named_groups_parse_and_unknown_names_are_rejected() -> None:
    assert toolsets.parse_toolsets("core, Firmware") == {"core", "firmware"}
    with pytest.raises(toolsets.ToolsetError, match="nope"):
        toolsets.parse_toolsets("core,nope")


def test_legacy_comes_only_when_named() -> None:
    assert toolsets.parse_toolsets("all,legacy") == toolsets.known_toolsets()
    assert toolsets.parse_toolsets("core,legacy") == {"core", "legacy"}


def test_a_minus_entry_drops_a_group() -> None:
    assert toolsets.parse_toolsets("-android") == toolsets.known_toolsets() - {"legacy", "android"}
    assert toolsets.parse_toolsets("core,legacy,-legacy") == {"core"}
    with pytest.raises(toolsets.ToolsetError, match="nope"):
        toolsets.parse_toolsets("-nope")


def test_the_old_device_reads_are_hidden_unless_legacy_is_named(server) -> None:
    names = _listed(server, {})
    assert "inspect" in names and "device_info" not in names and "get_config" not in names
    assert "device_info" in _listed(server, {toolsets.TOOLSETS_ENV: "all,legacy"})


def test_classification_of_known_tools() -> None:
    assert toolsets.toolset_of("inspect") == "core"
    assert toolsets.toolset_of("device_info") == "legacy"
    assert toolsets.toolset_of("build_start") == "firmware"
    assert toolsets.toolset_of("esptool_raw") == "hardware"
    assert toolsets.toolset_of("nrf52_flash_uf2") == "hardware"
    assert toolsets.toolset_of("android_tap") == "android"
    assert toolsets.toolset_of("atak_fleet_up") == "android"
    assert toolsets.toolset_of("cot_relay_start") == "replay"
    assert toolsets.toolset_of("vanity_preview") == "rf"
    assert toolsets.toolset_of("rf_confirm_tx") == "rf"
    assert toolsets.toolset_of("vision_oracle") == "local-model"
    assert toolsets.toolset_of("ios_boot") == "apple"
    assert toolsets.toolset_of("screenshot_set_start") == "apple"
    assert toolsets.toolset_of("sdk_send_text") == "sdk"
    assert toolsets.toolset_of("serial_open") == "hardware"
    assert toolsets.toolset_of("capture_screen") == "hardware"


def test_firmware_group_matches_the_servers_firmware_gate(server) -> None:
    assert toolsets._GROUPS["firmware"] == frozenset(server._FIRMWARE_TOOLS)


def test_android_group_covers_the_servers_android_gate(server) -> None:
    assert {n for n in server._ANDROID_TOOLS if toolsets.toolset_of(n) != "android"} == set()


def test_apple_group_covers_the_servers_apple_gate(server) -> None:
    assert {n for n in server._APPLE_TOOLS if toolsets.toolset_of(n) != "apple"} == set()


def test_rf_group_covers_the_servers_sdr_and_sniffer_gates(server) -> None:
    gated = {*server._SDR_TOOLS, *server._BLE_SNIFFER_TOOLS, *server._VANITY_TOOLS}
    assert {n for n in gated if toolsets.toolset_of(n) != "rf"} == set()


def _listed(server, env: dict[str, str]) -> set[str]:
    transform = toolsets.ToolsetTransform(env, lambda: server._READ_ONLY)
    tools = asyncio.run(server.app.list_tools())
    return {t.name for t in asyncio.run(transform.list_tools(tools))}


def test_default_lists_everything_but_legacy(server) -> None:
    everything = {t.name for t in asyncio.run(server.app.list_tools())}
    legacy = {n for n in everything if toolsets.toolset_of(n) == "legacy"}
    assert legacy and _listed(server, {}) == everything - legacy


def test_toolsets_filter_to_the_named_groups(server) -> None:
    names = _listed(server, {toolsets.TOOLSETS_ENV: "core"})
    assert names
    assert {toolsets.toolset_of(n) for n in names} == {"core"}
    assert "inspect" in names


def test_the_nrf52_bootloader_tools_come_with_hardware_not_core(server) -> None:
    nrf52 = {t.name for t in asyncio.run(server.app.list_tools()) if t.name.startswith("nrf52_")}
    assert nrf52
    assert not nrf52 & _listed(server, {toolsets.TOOLSETS_ENV: "core"})
    assert nrf52 <= _listed(server, {toolsets.TOOLSETS_ENV: "hardware"})


def test_read_only_hides_every_mutating_tool(server) -> None:
    names = _listed(server, {toolsets.READ_ONLY_ENV: "1"})
    assert names and names <= set(server._READ_ONLY)
    assert "factory_reset" not in names and "list_devices" in names


def test_hidden_tool_cannot_be_resolved(server) -> None:
    transform = toolsets.ToolsetTransform({toolsets.READ_ONLY_ENV: "1"}, lambda: server._READ_ONLY)

    async def call_next(name, *, version=None):
        return object()

    assert asyncio.run(transform.get_tool("factory_reset", call_next)) is None
    assert asyncio.run(transform.get_tool("list_devices", call_next)) is not None
