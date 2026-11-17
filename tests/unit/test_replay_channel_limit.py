# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Traffic only rides the channel slots the replay device advertised."""

from __future__ import annotations

import sqlite3
import threading
import time

from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import Capture, ReplayParams, ReplaySession, build, capture
from meshtastic_mcp.replay.capture import NodeRow
from meshtastic_mcp.replay.engine import MAX_CHANNELS

NODE = 0x10203040
START = 1_700_000_000
NAMES = ["LongFast", *(f"ch{i}" for i in range(1, MAX_CHANNELS + 2))]  # two past the limit


def _text(pid: int) -> mesh_pb2.MeshPacket:
    mp = build.packet(1, b"hi", from_node=NODE)
    mp.id = pid
    return mp


def _session(cap: Capture) -> ReplaySession:
    params = ReplayParams(host="127.0.0.1", port=0, rate=1000, stats_interval=0, mdns=False)
    return ReplaySession("slots", cap, params)


def _streamed(sess: ReplaySession) -> dict[int, int]:
    """Packet id -> channel slot for everything the stream sent from NODE."""
    sent: list[mesh_pb2.FromRadio] = []
    sess._stream(sent.append, None)
    return {
        f.packet.id: f.packet.channel
        for f in sent
        if f.HasField("packet") and getattr(f.packet, "from") == NODE
    }


def test_stream_drops_traffic_on_channels_past_the_slot_limit():
    packets = [
        (START + i, _text(100 + i).SerializeToString(), name) for i, name in enumerate(NAMES)
    ]
    cap = Capture(nodes=[NodeRow(NODE, f"!{NODE:08x}")], channels=NAMES, packets=packets)
    assert _streamed(_session(cap)) == {100 + i: i for i in range(MAX_CHANNELS)}


def test_inject_drops_packets_for_a_channel_past_the_slot_limit():
    cap = Capture(nodes=[NodeRow(NODE, f"!{NODE:08x}")], channels=NAMES, packets=[])
    sess = _session(cap)
    sent: list[mesh_pb2.FromRadio] = []
    sess.inject([_text(1)], channel=NAMES[-1])  # past the limit
    sess.inject([_text(2)], channel="ch3")
    sess.inject([_text(3)], channel="NotInCapture")  # unknown names keep the primary slot
    loop = threading.Thread(target=sess._inject_loop, args=(sent.append,), daemon=True)
    loop.start()
    deadline = time.time() + 5
    while len(sent) < 2 and time.time() < deadline:
        time.sleep(0.01)
    sess.state.stop.set()
    loop.join(timeout=2)
    assert {f.packet.id: f.packet.channel for f in sent} == {2: 3, 3: 0}


def _write_db(path, rows: list[tuple[int, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE node (id VARCHAR PRIMARY KEY, node_id BIGINT UNIQUE, long_name VARCHAR,
            short_name VARCHAR, hw_model VARCHAR, role VARCHAR, last_lat BIGINT,
            last_long BIGINT, last_update DATETIME);
        CREATE TABLE packet (id BIGINT PRIMARY KEY, payload BLOB, channel VARCHAR);
        CREATE TABLE packet_seen (packet_id BIGINT, rx_time BIGINT);
        """
    )
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?,?,?)",
        (f"!{NODE:08x}", NODE, "Node", "NODE", None, None, None, None, 1),
    )
    for pid, name in rows:
        conn.execute(
            "INSERT INTO packet VALUES (?,?,?)", (pid, _text(pid).SerializeToString(), name)
        )
        conn.execute("INSERT INTO packet_seen VALUES (?,?)", (pid, START + pid))
    conn.commit()
    conn.close()


def test_sqlite_channels_past_the_limit_never_land_on_the_primary_slot(tmp_path):
    # One packet per channel, plus extra LongFast traffic so it is the busiest.
    rows = [(i + 1, name) for i, name in enumerate(NAMES)] + [(50, "LongFast"), (51, "LongFast")]
    db = tmp_path / "many.db"
    _write_db(db, rows)
    cap = capture.from_sqlite(db, limit_nodes=0)
    name_of = dict(rows)
    slots = _streamed(_session(cap))
    retained = cap.channels[:MAX_CHANNELS]
    assert {name_of[pid] for pid in slots} == set(retained)
    assert all(retained[slot] == name_of[pid] for pid, slot in slots.items())
