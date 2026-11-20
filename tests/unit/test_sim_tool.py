# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`sim` and `sim_status` front the replay tools: same arguments in, same results out."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from meshtastic_mcp import toolsets


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


@pytest.fixture
def calls(monkeypatch):
    """Replace the old tools `sim` dispatches to with recorders."""
    from meshtastic_mcp.tools import replay

    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_start(**kwargs: Any) -> dict[str, Any]:
        seen.append(("start", kwargs))
        return {"id": "replay-9", "listen": "0.0.0.0:0"}

    def fake_stop(session_id: str | None = None) -> dict[str, Any]:
        seen.append(("stop", {"session_id": session_id}))
        return {"stopped": ["replay-9"]}

    def fake_status(session_id: str | None = None) -> dict[str, Any]:
        seen.append(("status", {"session_id": session_id}))
        return {"sessions": []}

    monkeypatch.setattr(replay, "replay_start", fake_start)
    monkeypatch.setattr(replay, "replay_stop", fake_stop)
    monkeypatch.setattr(replay, "replay_status", fake_status)
    return seen


def test_start_passes_only_what_was_given(server, calls) -> None:
    result = server.sim(action="start", source="defcon", port=0, loop=False, fuzz="parser")
    assert result == {"id": "replay-9", "listen": "0.0.0.0:0"}
    assert calls == [("start", {"source": "defcon", "port": 0, "loop": False, "fuzz": "parser"})]


def test_start_with_no_options_leaves_every_default_to_replay_start(server, calls) -> None:
    server.sim(action="start")
    assert calls == [("start", {})]


def test_stop_reaches_replay_stop(server, calls) -> None:
    assert server.sim(action="stop", session_id="replay-3") == {"stopped": ["replay-9"]}
    assert server.sim(action="stop") == {"stopped": ["replay-9"]}
    assert calls == [("stop", {"session_id": "replay-3"}), ("stop", {"session_id": None})]


def test_stop_refuses_start_parameters(server, calls) -> None:
    with pytest.raises(ToolError, match=r"session_id.*\['port', 'source'\]"):
        server.sim(action="stop", source="defcon", port=4403)
    assert calls == []


def test_start_refuses_session_id(server, calls) -> None:
    with pytest.raises(ToolError, match="session_id"):
        server.sim(action="start", session_id="replay-1")
    assert calls == []


def test_sim_status_reaches_replay_status(server, calls) -> None:
    assert server.sim_status("replay-2") == {"sessions": []}
    assert calls == [("status", {"session_id": "replay-2"})]


def test_toolset_classification() -> None:
    for name in ("sim", "sim_status"):
        assert toolsets.toolset_of(name) == "replay"
    for name in ("replay_start", "replay_stop", "replay_status"):
        assert toolsets.toolset_of(name) == "legacy"
    assert toolsets.toolset_of("replay_inject") == "replay"


def test_annotations(server) -> None:
    tools = {t.name: t for t in asyncio.run(server.app.list_tools())}
    assert tools["sim"].annotations.read_only_hint is False
    assert tools["sim"].annotations.destructive_hint is True
    assert tools["sim_status"].annotations.read_only_hint is True
    assert "sim" in server._DESTRUCTIVE and "sim_status" in server._READ_ONLY


def test_a_localhost_session_starts_reads_and_stops(server) -> None:
    started = server.sim(
        action="start",
        source="meshcon",
        host="127.0.0.1",
        port=0,
        sim_nodes=3,
        sim_days=1,
        mdns=False,
    )
    sid = started["id"]
    try:
        assert started["listen"].startswith("127.0.0.1:")
        one = server.sim_status(sid)
        assert one["id"] == sid and one["ended"] is False
        assert sid in [s["id"] for s in server.sim_status()["sessions"]]
        assert server.sim_status("no-such")["error"]
    finally:
        stopped = server.sim(action="stop", session_id=sid)
    assert stopped == {"stopped": [sid]}
    assert server.sim_status(sid)["error"]


def test_results_satisfy_the_output_schemas(server) -> None:
    from fastmcp import Client

    async def run() -> list[Any]:
        async with Client(server.app) as client:
            out = []
            start = {
                "action": "start",
                "host": "127.0.0.1",
                "port": 0,
                "sim_nodes": 3,
                "sim_days": 1,
                "mdns": False,
            }
            started = await client.call_tool("sim", start, raise_on_error=False)
            out.append(started)
            sid = started.structured_content["id"]
            for name, args in (
                ("sim_status", {}),
                ("sim_status", {"session_id": sid}),
                ("sim_status", {"session_id": "no-such"}),
                ("sim", {"action": "stop"}),
            ):
                out.append(await client.call_tool(name, args, raise_on_error=False))
            return out

    results = asyncio.run(run())
    assert [r.is_error for r in results] == [False] * 5, [r.content for r in results]
