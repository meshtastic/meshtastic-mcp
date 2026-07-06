# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture statistics for sim calibration and realism regression.

Computes a common, JSON-serializable stat schema over any
:class:`~meshtastic_mcp.replay.capture.Capture` — real (SQLite / recorder
JSONL) or synthetic (:mod:`~meshtastic_mcp.replay.sim`) — so the generator can
be measured against real event captures (Burning Man 2025, DEF CON 33) on
identical axes. The distilled aggregates for those two events live in
``replay/profiles/*.json`` — generated locally from the (private) datasets and
gitignored, never committed. They double as regression baselines and
calibration inputs; tests that need them skip when absent.

``capture_stats`` walks ``Capture.packets`` once; ``sqlite_extra_stats`` adds
the observation-level stats only the ``packet_seen`` table has (per-gateway
duplicate multiplicity, RX SNR/RSSI populations).
"""

from __future__ import annotations

import itertools
import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from meshtastic.protobuf import mesh_pb2, telemetry_pb2

from .capture import Capture

BROADCAST = 0xFFFFFFFF
SCHEMA_VERSION = 1

# Friendly names for the portnums that matter here (see portnums.proto).
PORT_NAMES = {
    1: "TEXT_MESSAGE",
    3: "POSITION",
    4: "NODEINFO",
    5: "ROUTING",
    6: "ADMIN",
    8: "WAYPOINT",
    34: "PAXCOUNTER",
    37: "MESH_BEACON",
    65: "STORE_FORWARD",
    66: "RANGE_TEST",
    67: "TELEMETRY",
    70: "TRACEROUTE",
    71: "NEIGHBORINFO",
    72: "ATAK_PLUGIN",
    78: "ATAK_PLUGIN_V2",
    257: "ATAK_FORWARDER",
}


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))
    return sorted_vals[k]


def summarize(vals: list[float]) -> dict[str, Any]:
    """n/min/p50/p90/p99/max summary of a numeric population (NaNs dropped)."""
    v = sorted(x for x in vals if not math.isnan(x))
    return {
        "n": len(v),
        "min": v[0] if v else None,
        "p50": _percentile(v, 50),
        "p90": _percentile(v, 90),
        "p99": _percentile(v, 99),
        "max": v[-1] if v else None,
    }


def talker_skew(per_sender: Counter[int]) -> dict[str, Any]:
    """Traffic concentration: share of packets from the top 1% / 10% of senders."""
    counts = sorted(per_sender.values(), reverse=True)
    total = sum(counts) or 1
    n = len(counts)
    return {
        "senders": n,
        "top1pct_share": round(sum(counts[: max(1, n // 100)]) / total, 3),
        "top10pct_share": round(sum(counts[: max(1, n // 10)]) / total, 3),
    }


def _named_portnums(portnums: Counter[int]) -> dict[str, int]:
    return {PORT_NAMES.get(pn, str(pn)): c for pn, c in portnums.most_common()}


def capture_stats(cap: Capture) -> dict[str, Any]:
    """Single-pass stat schema over a capture (see module docstring)."""
    portnums: Counter[int] = Counter()
    hop_limit: Counter[int] = Counter()
    hop_start: Counter[int] = Counter()
    senders: Counter[int] = Counter()
    pkt_ids: Counter[int] = Counter()
    hod: Counter[int] = Counter()
    variant_mix: Counter[str] = Counter()
    env_fields: Counter[str] = Counter()
    battery: Counter[int] = Counter()
    precision: Counter[int] = Counter()
    text_lens: list[float] = []
    chutil: list[float] = []
    air_util: list[float] = []
    env_temp: list[float] = []
    rx_snr: list[float] = []
    rx_rssi: list[float] = []
    times: list[int] = []
    pos_times: dict[int, list[int]] = defaultdict(list)
    total = encrypted = text_n = dm_text = want_resp = 0

    mp = mesh_pb2.MeshPacket()
    for rxt, raw, _ch in cap.packets:
        mp.Clear()
        try:
            mp.ParseFromString(raw)
        except Exception:
            continue
        total += 1
        frm = getattr(mp, "from")
        senders[frm] += 1
        hop_limit[mp.hop_limit] += 1
        hop_start[mp.hop_start] += 1
        if mp.id:
            pkt_ids[mp.id] += 1
        if mp.rx_snr:
            rx_snr.append(round(mp.rx_snr, 2))
        if mp.rx_rssi:
            rx_rssi.append(float(mp.rx_rssi))
        if rxt > 100_000:  # ignore epoch-0 garbage rows
            times.append(rxt)
            hod[int(rxt // 3600 % 24)] += 1
        if mp.WhichOneof("payload_variant") != "decoded":
            encrypted += 1
            continue
        pn = mp.decoded.portnum
        portnums[pn] += 1
        if mp.decoded.want_response:
            want_resp += 1
        if pn == 1:  # text
            text_n += 1
            text_lens.append(float(len(mp.decoded.payload)))
            if mp.to != BROADCAST:
                dm_text += 1
        elif pn == 3:  # position
            pos = mesh_pb2.Position()
            try:
                pos.ParseFromString(mp.decoded.payload)
            except Exception:
                continue
            precision[pos.precision_bits] += 1
            pos_times[frm].append(rxt)
        elif pn == 67:  # telemetry
            tel = telemetry_pb2.Telemetry()
            try:
                tel.ParseFromString(mp.decoded.payload)
            except Exception:
                continue
            variant = tel.WhichOneof("variant") or "?"
            variant_mix[variant] += 1
            if variant == "device_metrics":
                d = tel.device_metrics
                battery[min(d.battery_level, 101)] += 1
                chutil.append(round(d.channel_utilization, 1))
                air_util.append(round(d.air_util_tx, 2))
            elif variant == "environment_metrics":
                for fld, _v in tel.environment_metrics.ListFields():
                    env_fields[fld.name] += 1
                if tel.environment_metrics.HasField("temperature"):
                    env_temp.append(round(tel.environment_metrics.temperature, 1))

    times.sort()
    span_h = (times[-1] - times[0]) / 3600 if len(times) > 1 else 0.0
    interarrival = [float(b - a) for a, b in itertools.pairwise(times) if 0 <= b - a < 3600]
    pos_deltas: list[float] = []
    for ts in pos_times.values():
        ts.sort()
        pos_deltas += [float(b - a) for a, b in itertools.pairwise(ts) if 5 < b - a < 7200]
    dup_mult = Counter(pkt_ids.values())

    hw = Counter(n.hw_model or "?" for n in cap.nodes)
    role = Counter(n.role or "?" for n in cap.nodes)

    return {
        "schema": SCHEMA_VERSION,
        "label": cap.label,
        "packets": total,
        "span_hours": round(span_h, 1),
        "pkts_per_hour": round(total / span_h, 1) if span_h else None,
        "nodes": {
            "count": len(cap.nodes),
            "hw_top": hw.most_common(15),
            "role_top": role.most_common(),
        },
        "channels": list(cap.channels),
        "portnum_mix": _named_portnums(portnums),
        "encrypted_fraction": round(encrypted / total, 3) if total else None,
        "want_response_fraction": round(want_resp / total, 4) if total else None,
        "hop_limit": {str(k): v for k, v in hop_limit.most_common(10)},
        "hop_start": {str(k): v for k, v in hop_start.most_common(10)},
        "talker_skew": talker_skew(senders),
        "text": {
            "n": text_n,
            "len": summarize(text_lens),
            "dm_fraction": round(dm_text / text_n, 3) if text_n else None,
        },
        "telemetry": {
            "variant_mix": dict(variant_mix.most_common()),
            "chutil": summarize(chutil),
            "air_util": summarize(air_util),
            "battery_top": battery.most_common(8),
            "env_fields": dict(env_fields.most_common()),
            "env_temperature": summarize(env_temp),
        },
        "position": {
            "precision_bits": {str(k): v for k, v in precision.most_common(10)},
            "interval_s": summarize(pos_deltas),
        },
        "timing": {
            "interarrival_s": summarize(interarrival),
            "hour_of_day_utc": {str(h): hod[h] for h in sorted(hod)},
        },
        "rx": {"snr": summarize(rx_snr), "rssi": summarize(rx_rssi)},
        "dup_id_multiplicity": {str(k): v for k, v in dup_mult.most_common(8)},
        "tak_packets": portnums.get(72, 0) + portnums.get(78, 0) + portnums.get(257, 0),
    }


def sqlite_extra_stats(db_path: str) -> dict[str, Any]:
    """Observation-level stats only the ``packet_seen`` table carries.

    ``Capture.from_sqlite`` dedupes to one row per packet id (earliest
    reception), so per-observation duplicate multiplicity and the RX SNR/RSSI
    populations must be read from the DB directly.
    """
    conn = sqlite3.connect(db_path)
    try:
        mult = Counter(
            int(row[0])
            for row in conn.execute(
                "SELECT cnt FROM (SELECT COUNT(*) cnt FROM packet_seen GROUP BY packet_id)"
            )
        )
        snr = [
            float(r[0])
            for r in conn.execute("SELECT rx_snr FROM packet_seen WHERE rx_snr IS NOT NULL")
        ]
        rssi = [
            float(r[0])
            for r in conn.execute(
                "SELECT rx_rssi FROM packet_seen WHERE rx_rssi IS NOT NULL AND rx_rssi != 0"
            )
        ]
        return {
            "observation_multiplicity": {str(k): v for k, v in mult.most_common(8)},
            "rx": {"snr": summarize(snr), "rssi": summarize(rssi)},
        }
    finally:
        conn.close()
