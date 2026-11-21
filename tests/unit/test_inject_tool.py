# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`inject(kind, ...)` reaches the same implementation as each `replay_inject*` tool."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from meshtastic_mcp.replay import ReplayParams, sim
from meshtastic_mcp.tools import replay as replay_tools


class _Manager:
    """Records what reaches the replay manager, as bytes, so two calls compare equal."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def inject(self, sid, packets, *, channel="LongFast", fuzz=False):
        self.calls.append(("inject", sid, [p.SerializeToString() for p in packets], channel, fuzz))
        return {"id": sid, "queued": len(packets), "connected": False}

    def inject_fromradio(self, sid, messages):
        self.calls.append(("inject_fromradio", sid, [m.SerializeToString() for m in messages]))
        return {"id": sid, "queued": len(messages), "connected": False}


@pytest.fixture
def manager(monkeypatch) -> _Manager:
    mgr = _Manager()
    monkeypatch.setattr(replay_tools, "get_replay_manager", lambda: mgr)
    monkeypatch.setattr(replay_tools.replay_build, "_next_id", lambda: 7)
    return mgr


# kind -> (inject arguments, the old tool, its arguments)
_CASES: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {
    "text": (
        {"args": {"body": "hi"}, "from_node": 5, "to_node": 6, "channel": "Other", "count": 2},
        "replay_inject",
        {
            "kind": "text",
            "args": {"body": "hi"},
            "from_node": 5,
            "to_node": 6,
            "channel": "Other",
            "count": 2,
        },
    ),
    "position": (
        {"args": {"lat": 1.0, "lon": 2.0}, "fuzz": True},
        "replay_inject",
        {"kind": "position", "args": {"lat": 1.0, "lon": 2.0}, "fuzz": True},
    ),
    "nodeinfo": (
        {"args": {"id": "!0000abcd", "long_name": "N"}},
        "replay_inject",
        {"kind": "nodeinfo", "args": {"id": "!0000abcd", "long_name": "N"}},
    ),
    "raw": (
        {"args": {"portnum": 256, "payload_hex": "00ff"}},
        "replay_inject",
        {"kind": "raw", "args": {"portnum": 256, "payload_hex": "00ff"}},
    ),
    "packet": (
        {
            "packet_kind": "traceroute",
            "args": {"route": [1, 2, 3], "request_id": 5},
            "from_node": 3,
            "to_node": 4,
        },
        "replay_inject",
        {
            "kind": "traceroute",
            "args": {"route": [1, 2, 3], "request_id": 5},
            "from_node": 3,
            "to_node": 4,
        },
    ),
    "waypoint": (
        {
            "lat": 40.0,
            "lon": -74.0,
            "name": "W",
            "geofence_radius": 250,
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "notify_on_enter": True,
            "count": 2,
        },
        "replay_inject_waypoint",
        {
            "lat": 40.0,
            "lon": -74.0,
            "name": "W",
            "geofence_radius": 250,
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "notify_on_enter": True,
            "count": 2,
        },
    ),
    "beacon": (
        {"message": "join", "offer_channel_name": "Mesh", "offer_region": "EU_868"},
        "replay_inject_beacon",
        {"message": "join", "offer_channel_name": "Mesh", "offer_region": "EU_868"},
    ),
    "client_notification": (
        {"variant": "key_verification_final", "nonce": 9, "is_sender": True, "count": 3},
        "replay_inject_client_notification",
        {"variant": "key_verification_final", "nonce": 9, "is_sender": True, "count": 3},
    ),
    "fileinfo": (
        {"file_name": "/x.bin", "size_bytes": -1, "count": 2},
        "replay_inject_fileinfo",
        {"file_name": "/x.bin", "size_bytes": -1, "count": 2},
    ),
    "traceroute": (
        {"destination_node": 0xAB, "snr_towards": [4, 8]},
        "replay_inject_traceroute",
        {"destination_node": 0xAB, "snr_towards": [4, 8]},
    ),
}


def test_every_kind_has_a_case() -> None:
    assert set(_CASES) == set(replay_tools._INJECT_KIND_PARAMS)


@pytest.mark.parametrize("kind", sorted(_CASES))
def test_kind_matches_the_old_tool(manager, kind) -> None:
    new_args, old_name, old_args = _CASES[kind]
    new_result = replay_tools.inject("s1", kind, **new_args)
    new_calls, manager.calls = manager.calls, []
    old_result = getattr(replay_tools, old_name)("s1", **old_args)
    assert new_calls == manager.calls
    assert len(new_calls) == 1
    assert new_result == old_result
    assert set(new_result) == {"id", "queued", "connected"}


@pytest.mark.parametrize("kind", sorted(_CASES))
def test_defaults_match_the_old_tool(manager, kind) -> None:
    required = (*replay_tools._INJECT_REQUIRED.get(kind, ()), "args")
    minimal = {k: v for k, v in _CASES[kind][0].items() if k in required}
    old_name = _CASES[kind][1]
    old_args = {k: v for k, v in _CASES[kind][2].items() if k in required}
    if old_name == "replay_inject":
        old_args["kind"] = minimal.get("packet_kind", kind)
    replay_tools.inject("s1", kind, **minimal)
    new_calls, manager.calls = manager.calls, []
    getattr(replay_tools, old_name)("s1", **old_args)
    assert new_calls == manager.calls


def test_unknown_session_returns_the_old_error_shape() -> None:
    result = replay_tools.inject("nope", "text", args={"body": "x"})
    assert result == replay_tools.replay_inject("nope", "text", {"body": "x"})
    assert "error" in result


@pytest.mark.parametrize(
    ("kind", "stray"),
    [
        ("text", {"lat": 1.0}),
        ("text", {"message": "m"}),
        ("text", {"packet_kind": "raw"}),
        ("packet", {"geofence_radius": 5}),
        ("waypoint", {"args": {"body": "x"}}),
        ("waypoint", {"to_node": 3}),
        ("beacon", {"fuzz": True}),
        ("client_notification", {"channel": "LongFast"}),
        ("fileinfo", {"from_node": 1}),
        ("traceroute", {"count": 2}),
    ],
)
def test_a_parameter_from_another_kind_is_refused(manager, kind, stray) -> None:
    base = {
        k: v for k, v in _CASES[kind][0].items() if k in replay_tools._INJECT_REQUIRED.get(kind, ())
    }
    with pytest.raises(ToolError, match="does not take") as exc:
        replay_tools.inject("s1", kind, **base, **stray)
    assert next(iter(stray)) in str(exc.value)
    assert "it takes" in str(exc.value)
    assert manager.calls == []


@pytest.mark.parametrize("kind", ["waypoint", "fileinfo", "traceroute"])
def test_a_required_parameter_is_enforced(manager, kind) -> None:
    with pytest.raises(ToolError, match="needs"):
        replay_tools.inject("s1", kind)
    assert manager.calls == []


def test_inject_is_listed_in_replay_and_the_old_tools_in_legacy() -> None:
    from meshtastic_mcp import toolsets

    assert toolsets.toolset_of("inject") == "replay"
    for old in (
        "replay_inject",
        "replay_inject_beacon",
        "replay_inject_client_notification",
        "replay_inject_fileinfo",
        "replay_inject_traceroute",
        "replay_inject_waypoint",
    ):
        assert toolsets.toolset_of(old) == "legacy"


def test_inject_advertises_its_schema_and_a_short_description() -> None:
    from meshtastic_mcp import server

    tools = {t.name: t for t in asyncio.run(server.app.list_tools())}
    tool = tools["inject"]
    assert len(tool.description or "") < 4096
    assert tool.output_schema and "properties" in tool.output_schema
    assert tool.annotations is not None and tool.annotations.destructive_hint is True


def test_inject_reaches_a_running_session() -> None:
    from meshtastic_mcp.replay import get_manager

    cap = sim.generate(nodes=5, days=1, seed=1, start=1_700_000_000)
    mgr = get_manager()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sid = mgr.start(cap, ReplayParams(host="127.0.0.1", port=port, rate=600, node_delay=0))["id"]
    try:
        time.sleep(0.2)
        wp = replay_tools.inject(sid, "waypoint", lat=40.0, lon=-74.0, geofence_radius=100)
        fi = replay_tools.inject(sid, "fileinfo", file_name="/a.bin", size_bytes=3)
        assert wp["id"] == fi["id"] == sid
        assert wp["queued"] == fi["queued"] == 1
    finally:
        mgr.stop(sid)
