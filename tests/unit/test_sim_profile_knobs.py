# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Every sim knob is declared, validated, and backed by one shared model."""

from __future__ import annotations

import inspect

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import capture, sim

START = 1_700_000_000


def _payloads(cap, portnum: int) -> list[mesh_pb2.MeshPacket]:
    out = []
    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket.FromString(raw)
        if mp.decoded.portnum == portnum:
            out.append(mp)
    return out


def test_traceroute_hops_is_a_declared_profile_knob():
    assert tuple(sim.PROFILE["traceroute_hops"]) == (0, 3)
    cap = sim.generate(nodes=40, days=1, seed=7, start=START, profile={"traceroute_hops": (0, 0)})
    responses = [mp for mp in _payloads(cap, 70) if mp.decoded.request_id]
    assert responses
    for mp in responses:
        assert not mesh_pb2.RouteDiscovery.FromString(mp.decoded.payload).route


def test_unknown_beacon_preset_is_rejected():
    with pytest.raises(ValueError, match="NOT_A_PRESET"):
        sim.generate(
            nodes=10, days=1, seed=1, start=START, profile={"beacon_presets": ["NOT_A_PRESET"]}
        )
    with pytest.raises(ValueError, match="beacon_presets"):
        sim.generate(nodes=10, days=1, seed=1, start=START, profile={"beacon_presets": "SHORT"})


def test_valid_beacon_presets_still_reach_the_beacons():
    from meshtastic.protobuf import config_pb2, mesh_beacon_pb2

    cap = sim.generate(
        nodes=40, days=1, seed=7, start=START, profile={"beacon_presets": ["short_turbo"]}
    )
    beacons = [
        mesh_beacon_pb2.MeshBeacon.FromString(mp.decoded.payload) for mp in _payloads(cap, 37)
    ]
    assert beacons
    turbo = config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO
    assert {b.offer_preset for b in beacons} == {turbo}


def test_node_snr_reads_the_shared_tier_table(monkeypatch):
    assert sim.SNR_TIERS is capture.SNR_TIERS
    assert sim.SNR_WEIGHTS is capture.SNR_WEIGHTS
    monkeypatch.setattr(capture, "SNR_TIERS", ((1.0, 2.0), (30.0, 40.0)))
    monkeypatch.setattr(capture, "SNR_WEIGHTS", (1, 0))
    assert all(1.0 <= capture._node_snr(num) <= 2.0 for num in range(1, 2000, 7))


def test_path_relays_is_annotated_so_mypy_checks_its_body():
    sig = inspect.signature(sim._path_relays)
    assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values())
    assert sig.return_annotation is not inspect.Signature.empty
