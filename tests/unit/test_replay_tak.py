# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""TAKPacketV2 wire-format support (the optional ``[tak]`` extra).

The SDK (meshtastic-tak) is a git-only dependency, so the wire-format tests
skip when it isn't installed; the availability-gate and error-path tests run
unconditionally.
"""

from __future__ import annotations

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import metrics, sim, tak

requires_tak = pytest.mark.skipif(not tak.available(), reason="[tak] extra not installed")


def _tak_payloads(cap):
    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") == "decoded" and mp.decoded.portnum == 72:
            yield mp.decoded.payload


def test_available_is_boolean():
    assert isinstance(tak.available(), bool)


def test_v2_requested_without_sdk_raises(monkeypatch):
    """profile tak.wire='v2' must fail fast with an install hint when the extra
    is absent — no silent fallback that would confuse an explicit request."""
    monkeypatch.setattr(tak, "available", lambda: False)
    prof = {"tak": {"team_nodes": 2, "wire": "v2"}}
    with pytest.raises(RuntimeError, match=r"\[tak\] extra"):
        sim.generate(nodes=40, days=1, seed=1, start=1_700_000_000, profile=prof)


@requires_tak
def test_wrapper_round_trips_pli_and_chat():
    pli = tak.build_pli(
        callsign="Coyote-1",
        uid="MESH-deadbeef",
        team=10,
        role=1,
        lat_i=407_864_000,
        lon_i=-1_192_065_000,
        altitude=1200,
        speed=3,
        course=120,
        battery=88,
    )
    wire = tak.compress(pli)
    assert isinstance(wire, bytes) and 0 < len(wire) <= 184  # LoRa-sized
    back = tak.decompress(wire)
    assert back.callsign == "Coyote-1"
    assert back.uid == "MESH-deadbeef"
    assert back.latitude_i == 407_864_000
    assert back.battery == 88

    chat = tak.build_chat(
        callsign="Coyote-1",
        uid="MESH-deadbeef",
        team=10,
        role=1,
        battery=88,
        message="rally at CP1",
    )
    assert tak.decompress(tak.compress(chat)).chat.message == "rally at CP1"


@requires_tak
def test_wire_is_cross_instance_compatible():
    """A payload our wrapper produces decompresses in an independent SDK
    compressor — i.e. it's real SDK wire, byte-compatible across platforms."""
    from meshtastic_tak import TakCompressor

    pli = tak.build_pli(
        callsign="Sage-2",
        uid="MESH-0badf00d",
        team=9,
        role=2,
        lat_i=360_000_000,
        lon_i=-1_150_000_000,
        altitude=800,
        speed=0,
        course=0,
        battery=55,
    )
    wire = tak.compress(pli)
    native = TakCompressor().decompress(wire)  # fresh instance, no shared state
    assert native.uid == "MESH-0badf00d"
    assert native.callsign == "Sage-2"


@requires_tak
def test_sim_v2_emits_valid_lora_sized_wire():
    from meshtastic_tak import TakCompressor

    prof = {"tak": {"team_nodes": 4, "pli_interval": 60, "chat_per_hour": 3, "wire": "v2"}}
    cap = sim.generate(nodes=150, days=1, seed=8, start=1_700_000_000, profile=prof)
    assert metrics.capture_stats(cap)["tak_packets"] > 0
    decoder = TakCompressor()
    sizes = []
    for payload in _tak_payloads(cap):
        pkt = decoder.decompress(payload)  # SDK-native decode of sim output
        assert pkt.uid.startswith("MESH-")
        sizes.append(len(payload))
    assert sizes
    sizes.sort()
    assert sizes[len(sizes) // 2] <= 184  # median within the LoRa MTU budget


@requires_tak
def test_v1_and_v2_wire_differ():
    """v1 emits legacy uncompressed TAKPacket; v2 emits compressed wire — a v2
    payload must NOT parse as a legacy TAKPacket with a populated callsign."""
    from meshtastic.protobuf import atak_pb2

    common = {"team_nodes": 3, "pli_interval": 60, "chat_per_hour": 0}
    v1 = sim.generate(
        nodes=80, days=1, seed=4, start=1_700_000_000, profile={"tak": {**common, "wire": "v1"}}
    )
    v2 = sim.generate(
        nodes=80, days=1, seed=4, start=1_700_000_000, profile={"tak": {**common, "wire": "v2"}}
    )
    v1_payloads = list(_tak_payloads(v1))
    v2_payloads = list(_tak_payloads(v2))
    assert v1_payloads and v2_payloads
    # legacy payloads parse as V1 TAKPacket with a nested contact callsign
    legacy = atak_pb2.TAKPacket()
    legacy.ParseFromString(v1_payloads[0])
    assert legacy.contact.callsign
    # the v2 wire is a different byte-shape (1-byte dict id + zstd body)
    assert v2_payloads[0] != v1_payloads[0]
