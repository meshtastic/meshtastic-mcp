# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tool failures reach the client as protocol errors, and hints reach it on the wire."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name: str, args: dict):
    from fastmcp import Client

    async def run():
        async with Client(server.app) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def test_bad_argument_is_an_error_result_not_a_payload(server) -> None:
    result = _call(server, "cot_relay_start", {"session": "../escape"})
    assert result.is_error is True


def test_annotations_survive_the_wire(server) -> None:
    from fastmcp import Client

    async def run():
        async with Client(server.app) as client:
            return {t.name: t for t in await client.list_tools()}

    tools = asyncio.run(run())
    assert tools["factory_reset"].annotations.destructive_hint is True
    assert tools["list_devices"].annotations.read_only_hint is True
