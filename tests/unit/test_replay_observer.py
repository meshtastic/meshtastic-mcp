# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the observer / RF gateway model (replay/observer.py)."""

from __future__ import annotations

import math
from collections import Counter

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay.observer import (
    ObserverParams,
    _synthetic_position,
    observe,
)

OBS_LAT, OBS_LON = 40.0, -119.2
BROADCAST = 0xFFFFFFFF
T0 = 1_750_000_000


def _mk_packet(
    pkt_id: int,
    sender: int,
    t: int,
    payload: bytes = b"hello",
    hop_limit: int = 3,
) -> tuple[int, bytes, str]:
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", sender)
    mp.to = BROADCAST
    mp.id = pkt_id
    mp.hop_limit = hop_limit
    mp.hop_start = 3
    mp.decoded.portnum = 1
    mp.decoded.payload = payload
    return (t, mp.SerializeToString(), "LongFast")


def _pos_at(dist_m: float, bearing_rad: float = 0.0) -> tuple[float, float]:
    """A point ``dist_m`` meters from the observer at the given bearing."""
    earth = 6_371_000.0
    dlat = dist_m * math.cos(bearing_rad) / earth
    dlon = dist_m * math.sin(bearing_rad) / (earth * math.cos(math.radians(OBS_LAT)))
    return OBS_LAT + math.degrees(dlat), OBS_LON + math.degrees(dlon)


def _fleet(
    n: int, dist_lo: float, dist_hi: float
) -> tuple[list[tuple[int, bytes, str]], dict[int, tuple[float, float]]]:
    """n packets, unique ids, senders spread deterministically over [lo, hi] m."""
    packets: list[tuple[int, bytes, str]] = []
    positions: dict[int, tuple[float, float]] = {}
    for i in range(n):
        sender = 0x1000_0000 + (i % 200)
        frac = (i % 200) / 199.0 if n > 1 else 0.0
        positions[sender] = _pos_at(dist_lo + frac * (dist_hi - dist_lo), bearing_rad=i * 0.37)
        packets.append(_mk_packet(pkt_id=1_000_000 + i, sender=sender, t=T0 + i))
    return packets, positions


def _parse(blob: bytes) -> mesh_pb2.MeshPacket:
    mp = mesh_pb2.MeshPacket()
    mp.ParseFromString(blob)
    return mp


def test_determinism() -> None:
    packets, positions = _fleet(300, 100, 1200)
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=0)
    a = observe(packets, positions, params)
    b = observe(packets, positions, params)
    assert a == b  # byte-identical

    other = observe(packets, positions, ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=1))
    assert other != a


def test_multiplicity_calibration() -> None:
    packets, positions = _fleet(4000, 50, 1500)
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, mqtt_fraction=0.0, seed=42)
    out = observe(packets, positions, params)

    copies_per_id: Counter[int] = Counter()
    rssis: list[int] = []
    for _, blob, _ in out:
        mp = _parse(blob)
        copies_per_id[mp.id] += 1
        rssis.append(mp.rx_rssi)

    n_ids = len(copies_per_id)
    assert n_ids > 3000  # most packets at these ranges are heard
    mult = Counter(copies_per_id.values())
    expected = dict(params.dup_weights)
    for k, weight in expected.items():
        frac = mult.get(k, 0) / n_ids
        assert abs(frac - weight) <= 0.06, f"multiplicity {k}: {frac:.3f} vs {weight}"

    # Report-worthy sanity on the RSSI population.
    rssis.sort()
    p50 = rssis[len(rssis) // 2]
    assert -128 <= p50 <= -12


def test_distance_loss() -> None:
    near, near_pos = _fleet(2000, 100, 100)
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=7)
    out = observe(near, near_pos, params)
    heard_ids = {_parse(blob).id for _, blob, _ in out}
    assert len(heard_ids) / 2000 >= 0.95

    far, far_pos = _fleet(2000, 80_000, 80_000)
    out_far = observe(far, far_pos, params)
    far_ids = {_parse(blob).id for _, blob, _ in out_far}
    assert len(far_ids) / 2000 < 0.05


def test_rx_metadata() -> None:
    packets, positions = _fleet(800, 50, 1500)
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=3)
    out = observe(packets, positions, params)
    assert out

    t_by_id = {_parse(blob).id: t for t, blob, _ in packets}
    direct_by_id: Counter[int] = Counter()
    for rx_t, blob, _ in out:
        mp = _parse(blob)
        assert -128 <= mp.rx_rssi <= -12
        assert -20.75 <= mp.rx_snr <= 15.25
        assert (mp.rx_snr * 4) % 1 == pytest.approx(0.0, abs=1e-6)  # quarter-dB steps
        assert mp.rx_time >= t_by_id[mp.id]
        assert rx_t == mp.rx_time
        assert 0 <= mp.hop_limit <= 3
        if mp.relay_node == 0:
            direct_by_id[mp.id] += 1

    # Copies beyond the first carry a nonzero relay_node: at most one
    # relay-free (direct) copy per packet id.
    assert all(count <= 1 for count in direct_by_id.values())


def test_identity_preservation() -> None:
    packets, positions = _fleet(500, 50, 1500)
    originals = {_parse(blob).id: _parse(blob) for _, blob, _ in packets}
    out = observe(packets, positions, ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=5))
    assert out
    for _, blob, channel in out:
        mp = _parse(blob)
        orig = originals[mp.id]
        assert getattr(mp, "from") == getattr(orig, "from")
        assert mp.to == orig.to
        assert mp.id == orig.id
        assert mp.decoded.portnum == orig.decoded.portnum
        assert mp.decoded.payload == orig.decoded.payload
        assert channel == "LongFast"


def test_mqtt_mode() -> None:
    packets, positions = _fleet(300, 100, 1000)
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, mqtt_fraction=1.0, seed=9)
    out = observe(packets, positions, params)

    mqtt_ids = set()
    for _, blob, _ in out:
        mp = _parse(blob)
        if mp.via_mqtt:
            assert mp.rx_rssi == 0
            assert mp.rx_snr == 0.0
            assert mp.hop_limit == 3  # hop fields untouched on the bridged copy
            mqtt_ids.add(mp.id)
    input_ids = {_parse(blob).id for _, blob, _ in packets}
    assert mqtt_ids == input_ids  # every input yields at least one MQTT copy


def test_unknown_sender_fallback() -> None:
    packets = [
        _mk_packet(pkt_id=2_000_000 + i, sender=0x2000_0000 + (i % 50), t=T0 + i)
        for i in range(400)
    ]
    params = ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=11)
    a = observe(packets, {}, params)
    b = observe(packets, {}, params)
    assert a  # synthetic positions are near the observer, so output is nonempty
    assert a == b  # same synthetic position + same seed → identical output

    # The synthetic position depends only on the node number.
    p1 = _synthetic_position(0x2000_0000, OBS_LAT, OBS_LON)
    p2 = _synthetic_position(0x2000_0000, OBS_LAT, OBS_LON)
    assert p1 == p2
    assert _synthetic_position(0x2000_0001, OBS_LAT, OBS_LON) != p1


def test_sorted_output() -> None:
    packets, positions = _fleet(1000, 50, 1500)
    out = observe(packets, positions, ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=13))
    times = [t for t, _, _ in out]
    assert times == sorted(times)


def test_sim_observer_integration() -> None:
    """generate(profile={"observer": {"enabled": True}}) yields a gateway view:
    fewer unique ids than truth, duplicate copies, RX metadata, deterministic."""
    from meshtastic_mcp.replay import sim

    truth = sim.generate(nodes=120, days=1, seed=21, start=1_700_000_000)
    prof = {"observer": {"enabled": True, "mqtt_fraction": 0.2}}
    obs_a = sim.generate(nodes=120, days=1, seed=21, start=1_700_000_000, profile=prof)
    obs_b = sim.generate(nodes=120, days=1, seed=21, start=1_700_000_000, profile=prof)
    assert obs_a.packets == obs_b.packets  # seeded end-to-end

    ids = Counter()
    rf_meta = mqtt = 0
    for _, blob, _ in obs_a.packets:
        mp = _parse(blob)
        ids[mp.id] += 1
        if mp.via_mqtt:
            mqtt += 1
        elif mp.rx_rssi:
            assert -128 <= mp.rx_rssi <= -12
            rf_meta += 1
    truth_ids = {_parse(blob).id for _, blob, _ in truth.packets}
    assert 0 < len(ids) < len(truth_ids)  # loss happened
    assert any(v > 1 for v in ids.values())  # rebroadcast duplicates happened
    assert rf_meta > 0 and mqtt > 0


def test_fading_gate_creates_gap_tails() -> None:
    """The Gilbert-Elliott fading gate produces the heavy inter-arrival tails
    real gateway captures show (bursts of loss -> long silent gaps)."""
    packets, positions = _fleet(4000, 100, 800)
    base = ObserverParams(lat=OBS_LAT, lon=OBS_LON, seed=17)
    faded = ObserverParams(
        lat=OBS_LAT, lon=OBS_LON, seed=17, fade_good_s=120.0, fade_bad_s=90.0, fade_bad_loss=0.95
    )
    out_base = observe(packets, positions, base)
    out_faded = observe(packets, positions, faded)
    assert len(out_faded) < len(out_base)  # fading removes traffic

    def max_gap(rows):
        import itertools

        ts = [t for t, _b, _c in rows]
        return max((b - a for a, b in itertools.pairwise(ts)), default=0)

    # bad-state dwells carve visible silences into the stream
    assert max_gap(out_faded) > max_gap(out_base)
    assert max_gap(out_faded) >= 30
