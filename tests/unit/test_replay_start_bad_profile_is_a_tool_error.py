# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A bad `sim_profile` fails `replay_start` with a `ToolError` that names the cause."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from meshtastic_mcp.tools import replay as tools_replay

_BAD_PROFILES: list[Any] = [
    {"beacon_presets": "SHORT"},
    {"beacon_presets": ["NOT_A_PRESET"]},
    "[1, 2]",
    "{not json",
]


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _args(profile: Any) -> dict[str, Any]:
    return {
        "source": "meshcon",
        "port": 0,
        "sim_nodes": 3,
        "sim_days": 1,
        "sim_profile": profile,
        "mdns": False,
    }


@pytest.mark.parametrize("profile", _BAD_PROFILES)
def test_bad_sim_profile_raises_tool_error(server, profile: Any) -> None:
    with pytest.raises(ToolError):
        tools_replay.replay_start(**_args(profile))


def test_bad_sim_profile_reaches_the_client_as_its_own_message(server) -> None:
    from fastmcp import Client

    async def run() -> Any:
        async with Client(server.app) as client:
            return await client.call_tool(
                "replay_start", _args({"beacon_presets": "SHORT"}), raise_on_error=False
            )

    result = asyncio.run(run())
    text = result.content[0].text
    assert result.is_error is True
    assert "beacon_presets" in text
    assert not text.startswith("Error calling tool")
