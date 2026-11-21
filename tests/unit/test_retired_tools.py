# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A call to a hidden or removed tool name says what replaces it."""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP

from meshtastic_mcp import retired_tools, toolsets


def _app():
    app = FastMCP("t")

    @app.tool()
    def inspect() -> int:
        return 1

    app.add_middleware(retired_tools.RetiredToolMiddleware())
    return app


def _call(name):
    async def run():
        async with Client(_app()) as client:
            listed = {t.name for t in await client.list_tools()}
            result = await client.call_tool(name, {}, raise_on_error=False)
            return listed, result

    return asyncio.run(run())


def test_a_legacy_name_names_its_replacement_and_the_opt_in() -> None:
    listed, result = _call("device_info")
    text = result.content[0].text
    assert result.is_error is True
    assert "inspect(" in text and "MESHTASTIC_MCP_TOOLSETS=all,legacy" in text
    assert "device_info" not in listed


def test_a_discord_name_points_at_the_discord_server() -> None:
    _, result = _call("discord_search")
    assert result.is_error is True and "meshtastic-mcp-discord" in result.content[0].text


def test_a_local_model_name_says_it_was_removed() -> None:
    _, result = _call("summarize_window")
    assert result.is_error is True and "removed" in result.content[0].text


def test_an_unknown_name_stays_unknown() -> None:
    _, result = _call("no_such_tool")
    assert result.is_error is True and "Unknown tool" in result.content[0].text


def test_every_legacy_tool_has_a_replacement() -> None:
    assert set(retired_tools.REPLACED_BY) == set(toolsets._GROUPS["legacy"])
