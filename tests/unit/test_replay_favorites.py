# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Favorites reach the replay node DB from the sources that record them."""

from __future__ import annotations

from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import ReplayParams, ReplaySession, capture, sim

START = 1_700_000_000


def _node_db_favorites(cap) -> dict[int, bool]:
    sess = ReplaySession("fav", cap, ReplayParams(host="127.0.0.1", port=0, node_delay=0))
    frames: list[mesh_pb2.FromRadio] = []
    sess._send_node_db(frames.append)
    return {f.node_info.num: f.node_info.is_favorite for f in frames if f.HasField("node_info")}


def test_scripted_nodeinfo_marks_a_favorite():
    cap = capture.from_events(
        [
            {
                "kind": "nodeinfo",
                "from": 0x11,
                "args": {"long_name": "Pinned", "is_favorite": True},
            },
            {"kind": "nodeinfo", "from": 0x22, "args": {"long_name": "Other"}},
            {"kind": "text", "from": 0x33, "args": {"body": "hi"}},
        ],
        start=START,
    )
    assert {n.num: n.is_favorite for n in cap.nodes} == {0x11: True, 0x22: False, 0x33: False}
    assert _node_db_favorites(cap) == {0x11: True, 0x22: False, 0x33: False}


def test_sim_favorites_are_off_by_default():
    assert sim.PROFILE["favorite_fraction"] == 0.0
    cap = sim.generate(nodes=50, days=1, seed=4, start=START)
    assert not any(n.is_favorite for n in cap.nodes)


def test_sim_marks_favorites_at_the_profile_fraction():
    plain = sim.generate(nodes=300, days=1, seed=4, start=START)
    pinned = sim.generate(
        nodes=300, days=1, seed=4, start=START, profile={"favorite_fraction": 0.25}
    )
    share = sum(n.is_favorite for n in pinned.nodes) / len(pinned.nodes)
    assert 0.15 < share < 0.35
    # Marking favorites draws nothing from the generator: same nodes, same traffic.
    assert [(n.num, n.long_name, n.public_key) for n in pinned.nodes] == [
        (n.num, n.long_name, n.public_key) for n in plain.nodes
    ]
    assert pinned.packets == plain.packets
    db = _node_db_favorites(pinned)
    assert db == {n.num: n.is_favorite for n in pinned.nodes}
