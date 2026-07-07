# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Observer / RF gateway model for the synthetic mesh.

The synthetic generator (:mod:`~meshtastic_mcp.replay.sim`) emits *all traffic
that exists* on the mesh. Real event captures (Burning Man 2025, DEF CON 33)
are instead *what one gateway heard*: RF-lossy with distance, duplicated by
flood-rebroadcast (each copy with a decremented ``hop_limit``, its own
SNR/RSSI draw, and ``relay_node`` set), and stamped with rx metadata.
:func:`observe` transforms the omniscient packet stream into that observed
view.

Model
-----
Per input packet, the sender-to-observer distance feeds a log-distance path
loss with log-normal shadowing::

    rssi = tx_power - (ref_loss + 10 * n * log10(d)) + N(0, sigma)

Reception probability is a sigmoid around the radio sensitivity, scaled by a
residual ``loss_floor`` (collisions/duty-cycle losses that persist even at
point-blank range). Packets heard neither over RF nor via an optional MQTT
bridge are dropped entirely — this drop *is* the volume calibration that maps
"all traffic" onto a gateway-sized capture. RF-heard packets are duplicated
``k`` times per :attr:`ObserverParams.dup_weights` (rebroadcast copies arrive
later, hop-decremented, relayed, and usually stronger — relays tend to sit
closer to the gateway than the origin). SNR is derived from RSSI against the
noise floor and quantized to 0.25 dB, matching LoRa's quarter-dB reporting.

Calibration targets (measured from the real captures)
------------------------------------------------------
- DEF CON 33 duplicate multiplicity per packet id:
  {1: 70.8%, 2: 8.3%, 3: 18.9%, 4: 0.9%, 5+: ~1.1%} — the default
  ``dup_weights`` encode this fit.
- DEF CON 33 RSSI p50 −84 / p90 −53 / range [−123, −12];
  SNR p50 +10, range [−20.75, +15.25].
- Burning Man 2025 RSSI p50 −100; SNR p50 +3.0 — the sparser venue regime is
  reachable from the same model by tuning ``path_loss_exp`` / ``sigma_db``.
- DEF CON 33 saw ~40% of observations arrive ``via_mqtt`` (``mqtt_fraction``).

Everything is deterministic: one ``random.Random(params.seed)`` drives all
stochastic draws, except the synthetic fallback position for unknown senders,
which depends only on the node number (stable per node across calls and
seeds).
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass

from meshtastic.protobuf import mesh_pb2

_EARTH_RADIUS_M = 6_371_000.0
_RSSI_MIN, _RSSI_MAX = -128, -12
_SNR_MIN, _SNR_MAX = -20.75, 15.25


@dataclass(frozen=True)
class ObserverParams:
    """Tunables for the gateway observation model (defaults ≈ DEF CON 33)."""

    lat: float  # observer position, degrees
    lon: float
    tx_power_dbm: float = 20.0
    ref_loss_db: float = 40.0  # path loss at 1 m (915 MHz + margin)
    path_loss_exp: float = 2.9  # log-distance exponent
    sigma_db: float = 9.0  # log-normal shadowing stddev
    noise_floor_dbm: float = -103.0  # implied by real captures (rssi - snr ≈ -94..-103)
    snr_jitter_db: float = 2.0  # SNR is not perfectly rssi-correlated in reality
    sensitivity_dbm: float = -128.0
    softness_db: float = 5.0  # sigmoid width for reception probability
    loss_floor: float = 0.03  # residual loss even at point-blank range
    # copies heard per received packet (rebroadcast duplication), fit from DEF CON 33:
    dup_weights: tuple[tuple[int, float], ...] = (
        (1, 0.70),
        (2, 0.08),
        (3, 0.19),
        (4, 0.01),
        (5, 0.01),
        (6, 0.01),
    )
    mqtt_fraction: float = 0.0  # independent chance a copy also arrives via MQTT bridge
    # Gilbert-Elliott fading gate: the gateway alternates good/bad reception
    # states (collision storms, duty-cycle deafness, interference bursts).
    # ``fade_good_s`` == 0 disables fading. Fading is what produces the real
    # captures' heavy inter-arrival tails (p99 ~35 s, max gaps of minutes).
    fade_good_s: float = 0.0  # mean dwell in the good state (seconds)
    fade_bad_s: float = 45.0  # mean dwell in the bad state (seconds)
    fade_bad_loss: float = 0.85  # extra RF loss applied while in the bad state
    seed: int = 0


def _sigmoid(x: float) -> float:
    """Numerically stable logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular-approximation distance in meters (floored at 1 m)."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2.0))
    y = math.radians(lat2 - lat1)
    return max(1.0, _EARTH_RADIUS_M * math.hypot(x, y))


def _synthetic_position(node_num: int, obs_lat: float, obs_lon: float) -> tuple[float, float]:
    """Deterministic fallback position for a sender with no known coordinates.

    Hashes the node number to a bearing + distance within ~3 km of the
    observer. Depends only on ``node_num`` (never on the RNG seed), so the same
    unknown node lands in the same spot across calls.
    """
    digest = hashlib.sha256(f"meshtastic-mcp-observer:{node_num}".encode()).digest()
    bearing = int.from_bytes(digest[:4], "big") / 2**32 * 2.0 * math.pi
    dist_m = 50.0 + int.from_bytes(digest[4:8], "big") / 2**32 * 2950.0
    dlat = dist_m * math.cos(bearing) / _EARTH_RADIUS_M
    dlon = dist_m * math.sin(bearing) / (_EARTH_RADIUS_M * math.cos(math.radians(obs_lat)))
    return obs_lat + math.degrees(dlat), obs_lon + math.degrees(dlon)


def _relay_byte(packet_id: int, copy_index: int) -> int:
    """Plausible nonzero relay-node byte, stable per (packet id, copy index)."""
    digest = hashlib.sha256(f"meshtastic-mcp-relay:{packet_id}:{copy_index}".encode()).digest()
    return (digest[0] % 255) + 1


def _clamp_rssi(rssi_db: float) -> int:
    return max(_RSSI_MIN, min(_RSSI_MAX, round(rssi_db)))


def _quantize_snr(snr_db: float) -> float:
    """Clamp to LoRa's reportable range and quantize to quarter-dB steps."""
    return max(_SNR_MIN, min(_SNR_MAX, round(snr_db * 4.0) / 4.0))


def observe(
    packets: list[tuple[int, bytes, str]],
    positions: Mapping[int, tuple[float, float]],
    params: ObserverParams,
) -> list[tuple[int, bytes, str]]:
    """Filter and duplicate an omniscient packet stream into a gateway view.

    ``packets`` are ``(rx_time, serialized MeshPacket, channel_name)`` tuples
    as produced by the sim / stored in a :class:`~..capture.Capture`;
    ``positions`` maps node numbers to ``(lat, lon)`` degrees. Unknown senders
    get a deterministic synthetic position near the observer. The result is
    the subset (and rebroadcast multiplication) of the input the gateway would
    have heard, each copy stamped with ``rx_time`` / ``rx_snr`` / ``rx_rssi``
    (and ``relay_node`` / ``via_mqtt`` where applicable), sorted by rx time.
    Identity fields (from/to/id/portnum/payload) are never altered.
    """
    rng = random.Random(params.seed)
    dup_values = [k for k, _ in params.dup_weights]
    dup_wts = [w for _, w in params.dup_weights]
    out: list[tuple[int, bytes, str]] = []

    fading = params.fade_good_s > 0
    good = True
    switch_t = 0.0
    if fading:
        packets = sorted(packets, key=lambda p: p[0])
        if packets:
            switch_t = packets[0][0] + rng.expovariate(1.0 / params.fade_good_s)

    for t, raw, channel in packets:
        if fading:
            while t >= switch_t:
                good = not good
                dwell = params.fade_good_s if good else params.fade_bad_s
                switch_t += rng.expovariate(1.0 / dwell)
        mp = mesh_pb2.MeshPacket()
        try:
            mp.ParseFromString(raw)
        except Exception:
            continue  # unparseable input: skip
        sender = getattr(mp, "from")

        sender_pos = positions.get(sender)
        if sender_pos is None:
            sender_pos = _synthetic_position(sender, params.lat, params.lon)
        dist = _distance_m(params.lat, params.lon, sender_pos[0], sender_pos[1])

        # Log-distance path loss + log-normal shadowing.
        rssi = (
            params.tx_power_dbm
            - (params.ref_loss_db + 10.0 * params.path_loss_exp * math.log10(dist))
            + rng.gauss(0.0, params.sigma_db)
        )
        p_rx = (1.0 - params.loss_floor) * _sigmoid(
            (rssi - params.sensitivity_dbm) / params.softness_db
        )
        if fading and not good:
            p_rx *= 1.0 - params.fade_bad_loss
        rf_heard = rng.random() < p_rx
        mqtt_heard = rng.random() < params.mqtt_fraction
        if not rf_heard and not mqtt_heard:
            continue  # gateway never heard this packet — the volume calibration

        orig_hop = mp.hop_limit
        if rf_heard:
            k = rng.choices(dup_values, weights=dup_wts, k=1)[0]
            hop = orig_hop
            offset = 0.0
            for i in range(k):
                copy = mesh_pb2.MeshPacket()
                copy.CopyFrom(mp)
                if i == 0:
                    # Direct copy: at most one hop consumed on the way in.
                    hop = max(0, orig_hop - rng.randint(0, 1))
                    copy_rssi = rssi
                else:
                    # Rebroadcast: more hops burned, a relay stamped, usually
                    # stronger (relays sit closer to the gateway), and later.
                    hop = max(0, hop - rng.randint(1, 2))
                    copy.relay_node = _relay_byte(mp.id, i)
                    copy_rssi = min(rssi + rng.uniform(0.0, 15.0), float(_RSSI_MAX))
                    offset += rng.uniform(0.3, 4.0)
                copy.hop_limit = hop
                rx_time = int(t + offset)
                copy.rx_time = rx_time
                copy.rx_rssi = _clamp_rssi(copy_rssi)
                snr = copy_rssi - params.noise_floor_dbm + rng.gauss(0.0, params.snr_jitter_db)
                copy.rx_snr = _quantize_snr(snr)
                out.append((rx_time, copy.SerializeToString(), channel))

        if mqtt_heard:
            # Bridged copy: no radio metadata, hop fields untouched.
            copy = mesh_pb2.MeshPacket()
            copy.CopyFrom(mp)
            copy.via_mqtt = True
            rx_time = int(t + rng.uniform(0.5, 3.0))
            copy.rx_time = rx_time
            out.append((rx_time, copy.SerializeToString(), channel))

    out.sort(key=lambda item: item[0])
    return out
