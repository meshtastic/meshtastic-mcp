# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Mesh: traceroute from TX to RX round-trips with no intermediate hops.

3.0 has no traceroute message: TX asks RX for its device metrics with
`PACKET_RECORD_PATH` set (`MeshInterface.sendTraceRoute`), RX's firmware
mirrors the flag onto its reply (`MeshModule::setReplyTo`), and the reply's
path tail records the relays it passed on the way back. In a 2-device direct
mesh there are none: the reply takes zero hops, its tail is empty, and its
`relay_node` is RX's own suffix.

Validates the round-trip: request encoding, RX firmware dispatch, the reply
mirroring the path flag, and client-side decode through
`meshtastic.__init__.py::protocols[TELEMETRY_APP]` (which publishes the
`meshtastic.receive.telemetry` pubsub topic).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from meshtastic.mesh_interface import MeshInterface

from ._receive import ReceiveCollector, nudge_nodeinfo_port


@pytest.mark.timeout(240)
def test_traceroute_one_hop(mesh_pair: dict[str, Any]) -> None:
    """Runs for every directed pair. Asserts TX sends + RX responds, then
    inspects the reply's recorded path to confirm it is direct.

    Why the listener is on TX (not RX):
        The traceroute reply is addressed to TX (the original requester).
        The meshtastic Python client publishes `meshtastic.receive.telemetry`
        on the interface that received that reply — which is TX's iface.

    Why we ping RX's NodeInfo before sending:
        Traceroute requests are directed sends (wantResponse=True, specific
        destinationId) — subject to the same PKI_SEND_FAIL_PUBLIC_KEY trap
        as `test_direct_with_ack`. We open RX briefly to trigger the
        on-demand NodeInfo broadcast, then wait for TX's nodesByNum to
        populate RX's publicKey before calling sendTraceRoute.
    """
    tx_port = mesh_pair["tx"]["port"]
    rx_port = mesh_pair["rx"]["port"]
    rx_node_num = mesh_pair["rx"]["my_node_num"]
    tx_role = mesh_pair["tx_role"]
    rx_role = mesh_pair["rx_role"]
    assert rx_node_num is not None, f"{rx_role} my_node_num missing"

    with ReceiveCollector(tx_port, topic="meshtastic.receive.telemetry") as tx_listener:
        # Bilateral PKI warmup — traceroute requests are directed and
        # PKI-encrypted, so both sides need current pubkeys. See
        # `_receive.py::nudge_nodeinfo` and the test_direct_with_ack
        # comment for the full rationale (one-sided nudge lets err=35
        # PKI_UNKNOWN_PUBKEY slip through in whichever direction had
        # stale RX-side cache).
        nudge_nodeinfo_port(rx_port)  # RX via brief side-connection
        tx_listener.broadcast_nodeinfo_ping()  # TX via already-open iface

        # Poll TX's view of RX until the publicKey propagates. 45 s matches
        # the cap used in `test_direct_with_ack`; the re-nudge at 15 s
        # covers a LoRa collision on the first NodeInfo broadcast.
        pk_deadline = time.monotonic() + 45.0
        last_nudge = time.monotonic()
        last_rec: dict[str, Any] = {}
        while time.monotonic() < pk_deadline:
            last_rec = (tx_listener._iface.nodesByNum or {}).get(rx_node_num, {})
            if last_rec.get("user", {}).get("publicKey"):
                break
            if time.monotonic() - last_nudge > 15.0:
                nudge_nodeinfo_port(rx_port)
                tx_listener.broadcast_nodeinfo_ping()
                last_nudge = time.monotonic()
            time.sleep(1.0)
        else:
            pytest.fail(
                f"TX ({tx_role}) never saw RX ({rx_role}) public key within "
                f"45s; nodesByNum entry={last_rec!r}"
            )

        # sendTraceRoute blocks internally on `waitForTraceRoute` and raises
        # `MeshInterface.MeshInterfaceError` on timeout. One retry covers a
        # transient LoRa collision on either the request or the reply.
        ok = False
        for _attempt in range(2):
            try:
                tx_listener._iface.sendTraceRoute(
                    dest=rx_node_num,
                    hopLimit=3,
                )
                ok = True
                break
            except MeshInterface.MeshInterfaceError:
                time.sleep(5.0)
        assert ok, (
            f"sendTraceRoute {tx_role}→{rx_role} timed out twice; the mesh "
            f"may be saturated or RX is not answering the device-metrics request"
        )

        # sendTraceRoute already waited for the response internally, but
        # pubsub dispatch runs on the meshtastic-python reader thread —
        # give it a short grace window to queue the packet.
        packet = tx_listener.wait_for(
            lambda p: p.get("from") == rx_node_num,
            timeout=5.0,
        )
        assert packet is not None, (
            f"sendTraceRoute returned OK but no `receive.telemetry` reply "
            f"from RX (0x{rx_node_num:08x}) arrived via pubsub. Captured: "
            f"{tx_listener.snapshot()!r}"
        )

        # Inspect the reply's recorded path. `raw` is the MeshPacket protobuf.
        raw = packet["raw"]
        assert raw.flags & raw.PACKET_RECORD_PATH, (
            f"{rx_role}'s reply did not mirror PACKET_RECORD_PATH, so it recorded no "
            f"path (firmware without MeshModule::setReplyTo mirroring the flag?)"
        )
        hops = raw.hop_start - raw.hop_limit
        assert hops == 0, (
            f"the reply should come straight back on a 2-device direct mesh "
            f"({tx_role}<-{rx_role}); took {hops} hops"
        )
        assert raw.path == b"", f"a direct reply records no tail; got {raw.path.hex()}"
        assert raw.relay_node == (rx_node_num & 0xFF or 0x01), (
            f"a direct reply's relay_node is {rx_role}'s own suffix; got {raw.relay_node:#04x}"
        )
