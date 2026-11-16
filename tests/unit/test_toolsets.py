# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Toolset grouping and the transform that hides tools outside the requested groups."""

from __future__ import annotations

import asyncio

import pytest

from meshtastic_mcp import toolsets


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def test_unset_and_all_mean_every_group() -> None:
    assert toolsets.parse_toolsets(None) is None
    assert toolsets.parse_toolsets("") is None
    assert toolsets.parse_toolsets("ALL") is None


def test_named_groups_parse_and_unknown_names_are_rejected() -> None:
    assert toolsets.parse_toolsets("core, Firmware") == {"core", "firmware"}
    with pytest.raises(toolsets.ToolsetError, match="nope"):
        toolsets.parse_toolsets("core,nope")


def test_classification_of_known_tools() -> None:
    assert toolsets.toolset_of("device_info") == "core"
    assert toolsets.toolset_of("build_start") == "firmware"
    assert toolsets.toolset_of("esptool_raw") == "hardware"
    assert toolsets.toolset_of("android_tap") == "android"
    assert toolsets.toolset_of("atak_fleet_up") == "android"
    assert toolsets.toolset_of("discord_search") == "discord"
    assert toolsets.toolset_of("cot_relay_start") == "replay"
    assert toolsets.toolset_of("vanity_preview") == "rf"
    assert toolsets.toolset_of("vision_oracle") == "local-model"


def test_firmware_group_matches_the_servers_firmware_gate(server) -> None:
    assert toolsets._GROUPS["firmware"] == frozenset(server._FIRMWARE_TOOLS)


def test_android_group_covers_the_servers_android_gate(server) -> None:
    assert {n for n in server._ANDROID_TOOLS if toolsets.toolset_of(n) != "android"} == set()


def _listed(server, env: dict[str, str]) -> set[str]:
    transform = toolsets.ToolsetTransform(env, lambda: server._READ_ONLY)
    tools = asyncio.run(server.app.list_tools())
    return {t.name for t in asyncio.run(transform.list_tools(tools))}


def test_default_lists_everything(server) -> None:
    everything = {t.name for t in asyncio.run(server.app.list_tools())}
    assert _listed(server, {}) == everything


def test_toolsets_filter_to_the_named_groups(server) -> None:
    names = _listed(server, {toolsets.TOOLSETS_ENV: "core"})
    assert names
    assert {toolsets.toolset_of(n) for n in names} == {"core"}
    assert "device_info" in names and "discord_search" not in names


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
