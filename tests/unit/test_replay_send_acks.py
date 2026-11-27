# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A client's outbound packets get the QueueStatus and Routing answers firmware gives.

Expected values come from firmware: MeshService::sendToMesh (QueueStatus with the
packet id), ReliableRouter::perhapsGenerateImplicitAckForOwnOverheard (broadcast
implicit ack, from and to the local node), the destination's ACK for a unicast
(MeshModule::allocAckNak: ROUTING_APP, request_id, priority ACK) and
NextHopRouter::doRetransmissions (MAX_RETRANSMIT, from and to the local node).
"""

from __future__ import annotations

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

from meshtastic_mcp.replay import ReplayParams, ReplaySession, sim

BROADCAST = 0xFFFFFFFF
PKT_ID = 0x1234ABCD


def _session() -> ReplaySession:
    cap = sim.generate(nodes=5, days=1, seed=3, start=1_700_000_000)
    sess = ReplaySession("acks", cap, ReplayParams(host="127.0.0.1", port=0, node_delay=0))
    sess._ack_delay = (0, 0)
    return sess


def _text(to: int, want_ack: bool, pkt_id: int = PKT_ID) -> mesh_pb2.MeshPacket:
    pkt = mesh_pb2.MeshPacket()
    pkt.to = to
    pkt.id = pkt_id
    pkt.want_ack = want_ack
    pkt.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    pkt.decoded.payload = b"hi"
    return pkt


def _run(sess: ReplaySession, pkt: mesh_pb2.MeshPacket) -> list[mesh_pb2.FromRadio]:
    out: list[mesh_pb2.FromRadio] = []
    sess._handle_client_packet(pkt, out.append)
    return out


def _routing(fr: mesh_pb2.FromRadio) -> mesh_pb2.Routing:
    r = mesh_pb2.Routing()
    r.ParseFromString(fr.packet.decoded.payload)
    return r


def test_text_send_reports_queue_status_with_the_packet_id():
    """Regression: a send was dropped silently, so the client never saw it queued."""
    out = _run(_session(), _text(BROADCAST, want_ack=False))
    assert out[0].HasField("queueStatus")
    assert out[0].queueStatus.mesh_packet_id == PKT_ID
    assert out[0].queueStatus.res == 0  # ERRNO_OK


def test_no_want_ack_gets_only_queue_status():
    """Regression: acking a packet that did not ask for one."""
    sess = _session()
    for to in (BROADCAST, sess.capture.nodes[0].num):
        out = _run(sess, _text(to, want_ack=False))
        assert [fr.WhichOneof("payload_variant") for fr in out] == ["queueStatus"]


def test_broadcast_want_ack_gets_implicit_ack_from_local_node():
    """Regression: a broadcast send never resolved, the SDK timed out after 30 s."""
    sess = _session()
    out = _run(sess, _text(BROADCAST, want_ack=True))
    assert out[0].HasField("queueStatus")
    ack = out[1].packet
    me = sess.params.observer_num
    assert (getattr(ack, "from"), ack.to) == (me, me)
    assert ack.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP
    assert ack.decoded.request_id == PKT_ID
    assert _routing(out[1]).error_reason == mesh_pb2.Routing.Error.NONE


def test_unicast_want_ack_gets_ack_from_the_destination():
    """Regression: a DM to a synthetic node was never acked by that node."""
    sess = _session()
    dest = sess.capture.nodes[0].num & 0xFFFFFFFF
    out = _run(sess, _text(dest, want_ack=True))
    ack = out[1].packet
    assert getattr(ack, "from") == dest
    assert ack.to == sess.params.observer_num
    assert ack.decoded.request_id == PKT_ID
    assert _routing(out[1]).error_reason == mesh_pb2.Routing.Error.NONE


def test_unicast_to_unknown_node_gets_max_retransmit():
    """Regression: an unreachable destination must end in a NAK, not a hang."""
    sess = _session()
    out = _run(sess, _text(0x0BADF00D, want_ack=True))
    me = sess.params.observer_num
    assert (getattr(out[1].packet, "from"), out[1].packet.to) == (me, me)
    assert out[1].packet.decoded.request_id == PKT_ID
    assert _routing(out[1]).error_reason == mesh_pb2.Routing.Error.MAX_RETRANSMIT


def test_admin_request_still_answered_and_queue_reported():
    """Regression guard: the new queue status must not displace the admin response."""
    sess = _session()
    pkt = mesh_pb2.MeshPacket()
    pkt.id = 77
    pkt.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
    pkt.decoded.payload = admin_pb2.AdminMessage(get_owner_request=True).SerializeToString()
    out = _run(sess, pkt)
    assert out[0].queueStatus.mesh_packet_id == 77
    assert out[1].packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP
    assert out[1].packet.decoded.request_id == 77
