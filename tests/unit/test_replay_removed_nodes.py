# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A node the client deleted sends nothing after the delete and leaves the node counts."""

from __future__ import annotations

import threading

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

from meshtastic_mcp.replay import Capture, ReplayParams, ReplaySession, build, sim
from meshtastic_mcp.replay.capture import NodeRow

VICTIM = 0x0BADF00D
START = 1_700_000_000


def _remove_request(num: int) -> mesh_pb2.MeshPacket:
    req = mesh_pb2.MeshPacket()
    req.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
    req.decoded.payload = admin_pb2.AdminMessage(remove_by_nodenum=num).SerializeToString()
    return req


class _StallingSet(set):
    """Removed-node set whose first lookup of VICTIM stalls until the delete has landed.

    It opens the window between the sender check and the write: an unguarded check
    returns "not removed", the delete completes, and the write still goes out.
    """

    def __init__(self, checked: threading.Event, removed: threading.Event) -> None:
        super().__init__()
        self.checked, self.removed, self.stalled = checked, removed, False

    def __contains__(self, num: object) -> bool:
        hit = super().__contains__(num)
        if num == VICTIM and not self.stalled:
            self.stalled = True
            self.checked.set()
            self.removed.wait(0.5)
        return hit


def _race(sess: ReplaySession, run_sender) -> list[bool]:
    """Run a sender against a concurrent delete; whether the delete had landed at each write."""
    checked, removed = threading.Event(), threading.Event()
    sess._removed_nodes = _StallingSet(checked, removed)
    landed: list[bool] = []

    def send(fr: mesh_pb2.FromRadio) -> None:
        if fr.HasField("packet") and getattr(fr.packet, "from") == VICTIM:
            landed.append(removed.is_set())

    def delete() -> None:
        checked.wait(5)
        sess._handle_client_packet(_remove_request(VICTIM), lambda fr: None)
        removed.set()

    deleter = threading.Thread(target=delete, daemon=True)
    deleter.start()
    run_sender(send)
    deleter.join(timeout=5)
    assert removed.is_set()
    return landed


def _capture() -> Capture:
    mp = build.packet(1, b"hi", from_node=VICTIM)
    return Capture(
        nodes=[NodeRow(VICTIM, f"!{VICTIM:08x}")],
        channels=["LongFast"],
        packets=[(START, mp.SerializeToString(), "LongFast")],
    )


def _session() -> ReplaySession:
    params = ReplayParams(host="127.0.0.1", port=0, rate=1000, stats_interval=0, mdns=False)
    return ReplaySession("race", _capture(), params)


def test_stream_never_writes_a_packet_after_its_sender_is_deleted():
    sess = _session()
    assert _race(sess, lambda send: sess._stream(send, None)) == [False]


def test_inject_never_writes_a_packet_after_its_sender_is_deleted():
    sess = _session()

    def run(send):
        def stop_after_send(fr: mesh_pb2.FromRadio) -> None:
            send(fr)
            sess.state.stop.set()

        sess.inject([build.packet(1, b"hi", from_node=VICTIM)])
        loop = threading.Thread(target=sess._inject_loop, args=(stop_after_send,), daemon=True)
        loop.start()
        loop.join(timeout=5)

    assert _race(sess, run) == [False]


def test_removed_nodes_leave_the_node_counts():
    cap = sim.generate(nodes=6, days=1, seed=3, start=START)
    sess = ReplaySession("counts", cap, ReplayParams(host="127.0.0.1", port=0, node_delay=0))
    sess._handle_client_packet(_remove_request(cap.nodes[0].num), lambda fr: None)

    frames: list[mesh_pb2.FromRadio] = []
    sess._send_observer_stats(frames.append)
    telemetry = telemetry_pb2.Telemetry.FromString(frames[0].packet.decoded.payload)
    # Self-inclusive, like a device's own LocalStats: the observer plus the nodes left.
    assert telemetry.local_stats.num_online_nodes == len(cap.nodes)
    assert telemetry.local_stats.num_total_nodes == len(cap.nodes)

    frames.clear()
    sess._send_config_phase(frames.append)
    my_info = next(f.my_info for f in frames if f.HasField("my_info"))
    assert my_info.nodedb_count == len(cap.nodes) - 1
