# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Synthetic mesh generator (MeshCon).

Builds a fully synthetic, PII-free :class:`~meshtastic_mcp.replay.capture.Capture`
for a fictional Meshtastic conference — **MeshCon @ the Very Large Array (VLA),
New Mexico** — that the replay engine can stream like a real radio.

The generator is statistics-driven: node-DB size, hardware/role mix, channel
lineup, conference length, and a diurnal activity envelope (quiet overnight →
morning arrival ramp → daytime sessions → evening-social text spike). Every
Meshtastic portnum/flavor is represented (NodeInfo, Position, Telemetry
device+environment+power, Text, Routing ACKs, Traceroute, NeighborInfo,
Waypoint, PaxCounter, StoreForward, Admin).

The ``PROFILE`` dict holds the tunable parameters. When real captures (e.g. the
DEF CON datasets) are available, fit those distributions and drop the values in
here to make the simulation match observed reality. Everything is seeded:
a given ``(seed, nodes, days, start)`` reproduces byte-for-byte.
"""

from __future__ import annotations

import itertools
import math
import random
import time

from meshtastic.protobuf import (
    admin_pb2,
    config_pb2,
    mesh_pb2,
    paxcount_pb2,
    storeforward_pb2,
    telemetry_pb2,
)

from .build import MESH_BEACON_APP, beacon_payload
from .capture import Capture, NodeRow

BROADCAST = 0xFFFFFFFF

# ── Tunable profile (fit to real captures as they become available) ──────────
PROFILE: dict = {
    "venue": {"name": "VLA, New Mexico", "lat": 34.0784, "lon": -107.6184},
    # (label, lat, lon, spread_km, weight)
    "clusters": [
        ("Base Camp", 34.0784, -107.6184, 0.25, 46),
        ("North Arm", 34.1300, -107.6184, 0.60, 14),
        ("Southwest Arm", 34.0500, -107.6700, 0.60, 12),
        ("Southeast Arm", 34.0500, -107.5650, 0.60, 12),
        ("Datil Town", 34.1483, -107.8497, 0.40, 9),
        ("South Baldy", 33.9892, -107.1869, 0.30, 3),
        ("Davenport Hill", 34.2600, -107.5200, 0.30, 2),
        ("Rovers", 34.0784, -107.6184, 5.00, 2),
    ],
    # Hardware/role mixes below are the blended distributions observed across two
    # real ~1,600-node captures (Burning Man 2025 + DEF CON 33). Only the
    # aggregate proportions inform the sim — no real node identities are used.
    "hw_weights": [
        ("HELTEC_V3", 72),
        ("TRACKER_T1000_E", 65),
        ("RAK4631", 58),
        ("T_DECK", 28),
        ("T_ECHO", 24),
        ("HELTEC_MESH_NODE_T114", 12),
        ("TBEAM", 6),
        ("SEEED_WIO_TRACKER_L1", 5),
        ("SEEED_XIAO_S3", 3),
        ("STATION_G2", 3),
        ("NANO_G2_ULTRA", 1),
    ],
    # Role mix from the real node DBs: events run ~6-13 routers per 1600 nodes,
    # not dozens — the first 8 sim nodes are always infra, extras stay rare.
    "role_weights": [
        ("CLIENT", 880),
        ("CLIENT_MUTE", 40),
        ("TRACKER", 15),
        ("SENSOR", 10),
        ("ROUTER_CLIENT", 4),
        ("CLIENT_HIDDEN", 4),
        ("ROUTER", 3),
        ("ROUTER_LATE", 3),
    ],
    "channels": ["LongFast", "MeshCon", "Talks", "Swap", "Hax", "Staff"],
    # periodic intervals (seconds) — Meshtastic firmware-default cadences
    "pos_interval": {"mobile": 300, "router": 1800, "default": 900},
    "telemetry_interval": 1800,
    "nodeinfo_refresh": 10800,
    # NodeInfo economy — real meshes are NODEINFO-dominated (~35% of decoded
    # traffic) because arrivals trigger want_response exchange storms and nodes
    # keep re-requesting unknowns. Pairs per arrival + a background cadence.
    "nodeinfo_exchange_pairs": (2, 6),
    "nodeinfo_background_interval": 2700,
    # Routing ACK economy: acks per ack-eligible event (DM text, traceroute,
    # want_response exchange) plus a per-node background reliable-delivery hum.
    "ack_ratio": 0.8,
    "ack_background_interval": 3600,
    # NeighborInfo is off by default in real firmware — only infra + a sliver
    # of enthusiasts emit it (BM: 0.04% of traffic).
    "neighborinfo_interval": 21600,
    "neighborinfo_fraction": 0.005,
    # ~3.5% of real nodes carry environment sensors (they are mostly CLIENTs
    # with a BME/lux board attached, not SENSOR-role nodes).
    "env_sensor_fraction": 0.035,
    "env_interval": 10800,
    # Per-node radio config: most run the default 3 hops, a subpopulation
    # cranks it to 7 (observed at DEF CON), infra often uses 4.
    "hop_start_weights": [("3", 80), ("4", 8), ("7", 6), ("2", 4), ("5", 2)],
    # Text DMs are rare on-air (BM ~3.4%; DC DMs are PKI-invisible).
    "text_dm_fraction": 0.03,
    # Observed text volume across the real captures was ~2.2 msgs/hr per 150
    # nodes (gateway-observed, an undercount); 4 keeps conference channels lively
    # while staying close to reality.
    "text_base_msgs_per_hour": 4,
    # share of traffic that is encrypted/foreign (channels the viewer lacks keys
    # for) — real captures ran ~40%; a moderate default keeps it visible.
    "encrypted_fraction": 0.25,
    # Beacons (MESH_BEACON_APP): fraction of routers/infra that emit periodic
    # MESH_BEACON_APP packets, and the broadcast interval in seconds.
    "beacon_fraction": 0.4,
    "beacon_interval": 1800,
}

# Real text is short: median ~18 chars, p90 ~79. About half of messages are
# terse one-liners / acks / emoji, so the generator mixes these in with the
# themed lines below to match the observed length distribution (synthetic only).
_SHORT = [
    "here",
    "ack",
    "ttyl",
    "on my way",
    "👍",
    "🔥",
    "🫡",
    "copy",
    "anyone on?",
    "test",
    "hello mesh",
    "gm",
    "o/",
    "nice",
    "lol",
    "+1",
    "thanks!",
    "where you at",
    "radio check",
    "73",
    # medium one-liners — real short messages skew a bit longer (p50 ~18-25)
    "on my way back to camp",
    "meet at the coffee truck?",
    "anyone got a spare battery pack",
    "heading to the north arm now",
    "signal is great from up here",
    "save me a seat at the talk",
    "who else is hearing this hop",
    "back online, swapped batteries",
]

KM_PER_DEG_LAT = 110.574
_ROUTER_NAMES = [
    "VLA-Hub",
    "NorthArm-RTR",
    "SWArm-RTR",
    "SEArm-RTR",
    "Datil-Gate",
    "SouthBaldy-HI",
    "Davenport-HI",
    "BaseCamp-Core",
]
_ADJ = [
    "Solar",
    "Dusty",
    "Quartz",
    "Coyote",
    "Mesa",
    "Radio",
    "Photon",
    "Yagi",
    "Helix",
    "Lora",
    "Packet",
    "Sage",
    "Mojave",
    "Cactus",
    "Beacon",
    "Drift",
    "Static",
    "Cosmic",
    "Array",
    "Dipole",
]
_NOUN = [
    "Hopper",
    "Relay",
    "Pinger",
    "Nomad",
    "Scout",
    "Hauler",
    "Whip",
    "Node",
    "Wanderer",
    "Owl",
    "Jackrabbit",
    "Roadrunner",
    "Antenna",
    "Gremlin",
    "Spark",
    "Burro",
    "Tumbleweed",
    "Hawk",
]

_CHATTER = {
    "LongFast": [
        "good morning MeshCon ☀️ coffee truck is at base camp",
        "anyone else seeing the VLA hub at -118? insane range out here",
        "hop count to Datil is 2 from base camp, who's relaying?",
        "7000ft and the sunset is unreal tonight 🌄",
        "mesh is solid across the whole array, nice work routers",
        "battery at 14% gonna drop off for a bit, ttyl",
        "first contact from the South Baldy router! 41km 🎉",
        "{h} you copy? trying to range test toward Davenport",
    ],
    "MeshCon": [
        "📣 Keynote starts 10:00 in the Dish Dome",
        "📣 Lightning talks at 14:00, signup on the mesh waypoint",
        "📣 Evening social + antenna shootout at 19:00, BYO node",
        "📣 Bus to Datil dinner leaves 18:30 sharp from the lot",
        "📣 CTF scoreboard is live on the Hax channel",
    ],
    "Talks": [
        "great talk on RF propagation, slides on the swap channel?",
        "question about hop_limit tuning for dense meshes — anyone?",
        "the neighbor-info deep dive was 🔥",
        "loved the store&forward demo, did it survive the gap test",
    ],
    "Swap": [
        "WTS: RAK4631 w/ enclosure, $35, at table 7",
        "ISO a spare 915MHz antenna, will trade stickers",
        "trading a Heltec V3 for a Station G2, hmu",
        "spare LiPo 18650s, 4 for $10, find {h} at the SW arm",
    ],
    "Hax": [
        "CTF flag 3 is hidden in a waypoint description 👀",
        "decoded the paxcounter beacon, neat little payload",
        "scoreboard: {h} just took the lead, gg",
        "managed to traceroute the whole array in one shot, 5 hops",
    ],
    "Staff": [
        "router on the north arm dropped, sending someone up",
        "chutil on primary hit 38%, throttling beacons",
        "swapped the South Baldy battery, voltage healthy again",
    ],
}

_pid = itertools.count(0x10000001)


def _weighted(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0.0
    for v, w in pairs:
        upto += w
        if r <= upto:
            return v
    return pairs[-1][0]


def _enum(enum_type, name, default=0):
    try:
        return enum_type.Value(name)
    except Exception:
        return default


def _jitter(rng, lat, lon, spread_km):
    kmlon = 111.320 * math.cos(math.radians(lat))
    return (
        lat + rng.gauss(0, spread_km / 2) / KM_PER_DEG_LAT,
        lon + rng.gauss(0, spread_km / 2) / max(kmlon, 1e-6),
    )


def _activity(hod: float) -> float:
    if hod < 6:
        return 0.06
    if hod < 8:
        return 0.06 + 0.5 * (hod - 6) / 2
    if hod < 12:
        return 0.7
    if hod < 13:
        return 0.55
    if hod < 18:
        return 0.75
    if hod < 23:
        return 1.0
    return 0.3


def _text_env(hod: float) -> float:
    base = _activity(hod)
    if 18 <= hod < 23:
        return base * 1.6
    if 9 <= hod < 18:
        return base * 0.7
    return base * 0.25


# How many hops a packet has already taken when the observer hears it (drives
# hop_limit = hop_start - taken); matches the observed remaining-hop spread.
_HOPS_TAKEN_WEIGHTS = [(0, 30), (1, 28), (2, 22), (3, 14), (4, 5), (5, 1)]

# Per-portnum MeshPacket.priority (matches firmware behaviour: periodic beacons
# are BACKGROUND, text is DEFAULT, ACKs are ACK, admin/traceroute RELIABLE).
_PRIORITY = {1: 64, 5: 120, 6: 70, 70: 70}
_PRIORITY_DEFAULT = 10  # BACKGROUND


def _hops_taken(rng: random.Random, hop_start: int) -> int:
    return min(hop_start, int(_weighted(rng, [(str(h), w) for h, w in _HOPS_TAKEN_WEIGHTS])))


def _mp(
    frm,
    to,
    portnum,
    payload,
    hop_limit,
    hop_start,
    ch_idx,
    *,
    priority: int = 0,
    want_response: bool = False,
) -> bytes:
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", frm & 0xFFFFFFFF)
    mp.to = to & 0xFFFFFFFF
    mp.id = next(_pid) & 0xFFFFFFFF
    mp.hop_start = max(0, hop_start)
    mp.hop_limit = max(0, min(hop_limit, mp.hop_start))
    mp.channel = ch_idx
    if priority:
        mp.priority = priority
    mp.decoded.portnum = portnum
    mp.decoded.payload = payload
    if want_response:
        mp.decoded.want_response = True
    return mp.SerializeToString()


# Encrypted-payload sizes mirror the underlying portnum mix (position ~24-32 B,
# nodeinfo ~64-80 B, telemetry ~32 B, text variable with a long tail).
_ENC_LEN_WEIGHTS = [
    ("24", 30),
    ("32", 25),
    ("48", 15),
    ("64", 10),
    ("80", 8),
    ("120", 6),
    ("180", 4),
    ("232", 2),
]


def _mp_enc(rng, frm, hop_start, ch_hash) -> bytes:
    """An encrypted (undecodable) packet — models traffic on a channel/key the
    viewer lacks (DEF CON ran ~45% such 'foreign' traffic). ``ch_hash`` is the
    OTA channel-hash byte a real radio would report (not a settings index)."""
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", frm & 0xFFFFFFFF)
    mp.to = 0xFFFFFFFF
    mp.id = next(_pid) & 0xFFFFFFFF
    mp.hop_start = max(0, hop_start)
    mp.hop_limit = max(0, hop_start - _hops_taken(rng, hop_start))
    mp.channel = ch_hash & 0xFF
    base = int(_weighted(rng, _ENC_LEN_WEIGHTS))
    mp.encrypted = bytes(rng.randint(0, 255) for _ in range(max(16, base + rng.randint(-6, 6))))
    return mp.SerializeToString()


def generate(
    *,
    nodes: int = 800,
    days: int = 3,
    start: int | None = None,
    seed: int = 1337,
    channels: list[str] | None = None,
    text_scale: float = 1.0,
    profile: dict | None = None,
) -> Capture:
    """Generate a MeshCon capture in memory."""
    P = {**PROFILE, **(profile or {})}
    rng = random.Random(seed)
    chans = channels or list(P["channels"])
    ch_index = {c: i for i, c in enumerate(chans)}
    start_epoch = int(start if start is not None else time.time())
    end_epoch = start_epoch + days * 86400

    def hod(t):
        return ((t - start_epoch) / 3600.0) % 24

    # -- build nodes --
    node_rows: list[NodeRow] = []
    meta: list[dict] = []
    used: set[int] = set()
    cl_w = [(c, c[4]) for c in P["clusters"]]
    ridx = 0
    for i in range(nodes):
        while True:
            num = rng.randint(0x10000000, 0xEFFFFFFF)
            if num not in used:
                used.add(num)
                break
        if i < 8:
            role = "ROUTER" if i < 4 else "ROUTER_LATE"
            cl = P["clusters"][i % 7]
        else:
            role = _weighted(rng, P["role_weights"])
            cl = _weighted_cluster(rng, cl_w)
        if role in ("ROUTER", "ROUTER_LATE") and ridx < len(_ROUTER_NAMES):
            long = _ROUTER_NAMES[ridx]
            short = "".join(ch for ch in long if ch.isalnum())[:4].upper()
            ridx += 1
        else:
            a, n = rng.choice(_ADJ), rng.choice(_NOUN)
            long, short = f"{a} {n}", (a[:2] + n[:2]).title()
        lat, lon = _jitter(rng, cl[1], cl[2], cl[3])
        mobile = role == "TRACKER" or cl[0] == "Rovers"
        # Talkativeness: real meshes are heavily skewed (top ~1% of nodes carry a
        # third of all traffic). A lognormal tail + an infra boost reproduces it;
        # the factor scales a node's beacon cadence below.
        talk = min(25.0, max(0.25, rng.lognormvariate(0.0, 0.9)))
        infra = role.startswith("ROUTER") or role == "SENSOR"
        if role.startswith("ROUTER"):
            talk *= 8.0
        elif role == "SENSOR":
            talk *= 5.0
        # Presence/churn drives the real heavy skew: infrastructure persists the
        # whole event, but most attendees are transient (arrive any time, stay a
        # while, leave) so they emit only a handful of packets. join anytime;
        # presence is lognormal (median ~2h) clamped to the event length.
        event_h = days * 24
        if infra:
            join_h, stay_h = 0.0, float(event_h)
        else:
            join_h = rng.uniform(0, max(0.1, event_h - 0.5))
            stay_h = min(event_h - join_h, max(0.2, rng.lognormvariate(0.7, 1.0)))
        node_rows.append(
            NodeRow(
                num,
                f"!{num:08x}",
                long,
                short,
                _weighted(rng, P["hw_weights"]),
                role,
                int(lat * 1e7),
                int(lon * 1e7),
            )
        )
        meta.append(
            {
                "num": num,
                "lat_i": int(lat * 1e7),
                "lon_i": int(lon * 1e7),
                "role": role,
                "mobile": mobile,
                "talk": talk,
                "hop_start": int(_weighted(rng, P["hop_start_weights"])),
                "join_t": start_epoch + int(join_h * 3600),
                "leave_t": start_epoch + int((join_h + stay_h) * 3600),
            }
        )

    packets: list[tuple[int, bytes, str]] = []
    routers = [m for m in meta if m["role"].startswith("ROUTER")]
    sensors = [m for m, nr in zip(meta, node_rows, strict=False) if nr.role == "SENSOR"]
    by_num = {nr.num: nr for nr in node_rows}
    hop_starts = {m["num"]: m["hop_start"] for m in meta}

    def add(t, frm, to, pn, payload, ch="LongFast", hop=None, want_response=False):
        # hop_limit derives from the sender's configured hop_start minus a
        # realistic hops-already-taken draw; callers pass an explicit remaining
        # `hop` for DMs/ACKs/traceroute (clamped to the sender's hop_start).
        hs = hop_starts.get(frm & 0xFFFFFFFF, 3)
        hl = (hs - _hops_taken(rng, hs)) if hop is None else min(hop, hs)
        packets.append(
            (
                t,
                _mp(
                    frm,
                    to,
                    pn,
                    payload,
                    max(0, hl),
                    hs,
                    ch_index.get(ch, 0),
                    priority=_PRIORITY.get(pn, _PRIORITY_DEFAULT),
                    want_response=want_response,
                ),
                ch,
            )
        )

    # -- periodic per-node traffic (only while the node is present) --
    for m, nr in zip(meta, node_rows, strict=False):
        jt = m["join_t"]
        node_end = min(end_epoch, m["leave_t"])
        if jt < node_end:
            add(jt, m["num"], BROADCAST, 4, _pl_nodeinfo(nr))
            # arrival exchange storm: peers swap NodeInfo with the newcomer
            # (want_response request -> reply, sometimes a routing ACK). This
            # is what makes real meshes NODEINFO-dominated.
            k = rng.randint(*P["nodeinfo_exchange_pairs"])
            for peer in rng.sample(meta, min(len(meta), k + 1)):
                if peer["num"] == m["num"]:
                    continue
                t0 = jt + rng.randint(2, 90)
                peer_info = _pl_nodeinfo(by_num[peer["num"]])
                add(t0, peer["num"], m["num"], 4, peer_info, want_response=True)
                if rng.random() < 0.85:
                    add(t0 + rng.randint(1, 6), m["num"], peer["num"], 4, _pl_nodeinfo(nr))
                if rng.random() < P["ack_ratio"] * 0.4:
                    add(t0 + rng.randint(2, 9), m["num"], peer["num"], 5, _pl_routing_ack(), hop=0)
        talk = m["talk"]
        pos_iv = (
            P["pos_interval"]["mobile"]
            if m["mobile"]
            else P["pos_interval"]["router"]
            if m["role"].startswith("ROUTER")
            else P["pos_interval"]["default"]
        )
        # Talkativeness skews *social* traffic hard, but periodic beacons only
        # mildly (position cadence is device config, not personality) — real
        # position intervals sit near the firmware defaults (p50 ~10 min).
        beacon_talk = talk**0.35
        pos_iv = max(30, pos_iv / beacon_talk)
        tel_iv = max(60, P["telemetry_interval"] / beacon_talk)
        t = jt + rng.randint(0, 300)
        while t < node_end:
            if rng.random() < _activity(hod(t)) + 0.1:
                add(t, m["num"], BROADCAST, 3, _pl_position(rng, m, t))
            t += int(pos_iv * rng.uniform(0.8, 1.2))
        t = jt + rng.randint(0, 600)
        while t < node_end:
            if rng.random() < _activity(hod(t)) + 0.15:
                add(t, m["num"], BROADCAST, 67, _pl_tel_device(rng, t))
            t += int(tel_iv * rng.uniform(0.8, 1.2))
        t = jt + P["nodeinfo_refresh"]
        while t < node_end:
            add(t, m["num"], BROADCAST, 4, _pl_nodeinfo(nr))
            t += int(P["nodeinfo_refresh"] * rng.uniform(0.8, 1.2))
        # background NodeInfo exchanges: nodes keep requesting identities they
        # don't know (churn means there is always someone unknown around)
        t = jt + rng.randint(60, P["nodeinfo_background_interval"])
        while t < node_end:
            if rng.random() < _activity(hod(t)) + 0.2:
                peer = rng.choice(meta)
                if peer["num"] != m["num"]:
                    add(t, m["num"], peer["num"], 4, _pl_nodeinfo(nr), want_response=True)
                    if rng.random() < 0.8:
                        add(
                            t + rng.randint(1, 6),
                            peer["num"],
                            m["num"],
                            4,
                            _pl_nodeinfo(by_num[peer["num"]]),
                        )
            t += int(P["nodeinfo_background_interval"] * rng.uniform(0.7, 1.3))
        # background reliable-delivery ACK hum (routing traffic tracks want_ack
        # usage — BM ran ~12% ROUTING)
        t = jt + rng.randint(0, P["ack_background_interval"])
        while t < node_end:
            if rng.random() < _activity(hod(t)):
                other = rng.choice(meta)
                if other["num"] != m["num"]:
                    add(t, m["num"], other["num"], 5, _pl_routing_ack(), hop=0)
            t += int(P["ack_background_interval"] * rng.uniform(0.7, 1.3))

    # -- environment + power telemetry: sensors, a few routers, plus the ~3.5%
    # of ordinary nodes that carry an attached sensor board --
    env_extra = [m for m in meta if rng.random() < P["env_sensor_fraction"]]
    env_nodes = list({m["num"]: m for m in sensors + routers[:3] + env_extra}.values())
    for m in env_nodes:
        t = start_epoch + rng.randint(0, P["env_interval"])
        while t < end_epoch:
            add(t, m["num"], BROADCAST, 67, _pl_tel_env(rng, t))
            if rng.random() < 0.9:
                add(t + 5, m["num"], BROADCAST, 67, _pl_tel_power(rng, t))
            t += int(P["env_interval"] * rng.uniform(0.8, 1.2))

    # -- neighborinfo (off by default in real firmware: infra + a sliver) --
    nbr = routers + [m for m in meta if rng.random() < P["neighborinfo_fraction"]]
    for m in nbr:
        t = start_epoch + rng.randint(0, 7200)
        while t < end_epoch:
            others = [x for x in meta if x["num"] != m["num"]]
            k = min(len(others), rng.randint(2, 6))
            add(
                t,
                m["num"],
                BROADCAST,
                71,
                _pl_neighborinfo(rng, m["num"], rng.sample(others, k)),
                hop=4,
            )
            t += int(P["neighborinfo_interval"] * rng.uniform(0.85, 1.15))

    # -- traceroutes + routing ACKs --
    for _ in range(max(1, days * 24 * 12)):
        t = rng.randint(start_epoch, end_epoch - 1)
        if rng.random() > _activity(hod(t)):
            continue
        na, nb = rng.sample(meta, 2)
        relays = rng.sample(routers, min(len(routers), rng.randint(0, 3))) if routers else []
        route = [na["num"]] + [r["num"] for r in relays] + [nb["num"]]
        add(t, na["num"], nb["num"], 70, _pl_traceroute(rng, route), hop=len(route))
        if rng.random() < 0.7:
            add(t + rng.randint(1, 4), nb["num"], na["num"], 5, _pl_routing_ack(), hop=0)

    # -- text chatter --
    base = P["text_base_msgs_per_hour"] * (nodes / 150.0) * text_scale
    for hour in range(days * 24):
        t0 = start_epoch + hour * 3600
        n_msgs = int(rng.gauss(base * _text_env(hod(t0)), base * 0.25))
        for _ in range(max(0, n_msgs)):
            t = t0 + rng.randint(0, 3599)
            ch = _weighted(
                rng,
                [
                    ("LongFast", 34),
                    ("MeshCon", 10),
                    ("Talks", 16),
                    ("Swap", 14),
                    ("Hax", 14),
                    ("Staff", 6),
                ],
            )
            if ch not in ch_index:
                ch = "LongFast"
            sender = rng.choice(node_rows)
            text = _pick_text(rng, ch, node_rows)
            dm = rng.random() < P["text_dm_fraction"]
            to = rng.choice(node_rows).num if dm else BROADCAST
            add(t, sender.num, to, 1, text.encode("utf-8"), ch=ch)
            if dm and rng.random() < P["ack_ratio"]:
                add(t + rng.randint(1, 5), to, sender.num, 5, _pl_routing_ack(), ch=ch, hop=0)

    # -- waypoints (some with geofence fields for client enter/exit alert testing) --
    pois = [
        ("Coffee Truck", "☕ best brew at base camp", 9749, False),
        ("Registration", "Badge pickup + swag", 128221, False),
        ("Dish Dome", "Main stage / keynotes", 127963, True),  # geofenced
        ("Antenna Shootout", "Range contest 19:00", 128225, True),  # geofenced
        ("Afterparty", "🔥 fire pit + RF stories", 128293, False),
        ("Datil Dinner", "Bus loads here 18:30", 127858, False),
    ]
    for day in range(days):
        for name, desc, icon, geofenced in rng.sample(pois, min(len(pois), rng.randint(3, 6))):
            t = start_epoch + day * 86400 + rng.randint(7 * 3600, 20 * 3600)
            host = rng.choice(routers) if routers else rng.choice(meta)
            add(
                t,
                host["num"],
                BROADCAST,
                8,
                _pl_waypoint(rng, host, t, name, desc, icon, geofenced=geofenced),
                ch="MeshCon" if "MeshCon" in ch_index else "LongFast",
                hop=4,
            )

    # -- encrypted/foreign traffic: packets on channels the viewer can't decode,
    # from "heard but unknown" neighbor nodes (no NodeInfo). The fraction is of
    # the *final* stream (DEF CON ran ~45%; default keeps it moderate). --
    frac = P.get("encrypted_fraction", 0.0)
    if frac > 0 and packets:
        n_enc = int(len(packets) * frac / max(1e-6, 1.0 - frac))
        foreign = [rng.randint(0x10000000, 0xEFFFFFFF) for _ in range(max(5, nodes // 8))]
        foreign_hs = {f: int(_weighted(rng, P["hop_start_weights"])) for f in foreign}
        # a handful of foreign channels, each with a realistic OTA hash byte
        foreign_hashes = [rng.randint(1, 255) for _ in range(rng.randint(2, 4))]
        added = attempts = 0
        while added < n_enc and attempts < n_enc * 10:
            attempts += 1
            t = rng.randint(start_epoch, end_epoch - 1)
            # foreign mesh traffic is largely independent of our conference rhythm
            if rng.random() > _activity(hod(t)) * 0.4 + 0.6:
                continue
            f = rng.choice(foreign)
            packets.append(
                (t, _mp_enc(rng, f, foreign_hs[f], rng.choice(foreign_hashes)), "LongFast")
            )
            added += 1

    # -- range test: a couple of nodes beacon sequence numbers (DEF CON had ~4%) --
    for m in rng.sample(meta, min(len(meta), 2)):
        seq = 0
        t = start_epoch + rng.randint(0, 600)
        while t < end_epoch:
            if rng.random() < _activity(hod(t)):
                seq += 1
                add(t, m["num"], BROADCAST, 66, f"seq {seq}".encode())
            t += int(rng.uniform(150, 420))

    # -- paxcounter + storeforward + admin --
    for m in sensors[:3] or routers[:2]:
        t = start_epoch + rng.randint(0, 1800)
        while t < end_epoch:
            add(t, m["num"], BROADCAST, 34, _pl_pax(rng), hop=2)
            t += int(1800 * rng.uniform(0.8, 1.2))
    for m in routers[:2]:
        t = start_epoch + rng.randint(0, 900)
        while t < end_epoch:
            add(t, m["num"], BROADCAST, 65, _pl_sf(), hop=2)
            t += int(900 * rng.uniform(0.85, 1.15))
    if routers:
        op = routers[0]
        for _ in range(max(1, days * 3)):
            t = rng.randint(start_epoch, end_epoch - 1)
            tgt = rng.choice(routers)
            if tgt["num"] != op["num"]:
                add(
                    t,
                    op["num"],
                    tgt["num"],
                    6,
                    _pl_admin(),
                    ch="Staff" if "Staff" in ch_index else "LongFast",
                )

    # -- beacons (MESH_BEACON_APP = 37): a fraction of infra nodes periodically
    # broadcast MeshBeacon packets so clients can exercise the "discover and
    # join a beaconed mesh" flow without real hardware. --
    beacon_iv = P.get("beacon_interval", 1800)
    beacon_frac = P.get("beacon_fraction", 0.4)
    beacon_nodes = [m for m in routers if rng.random() < beacon_frac]
    for m in beacon_nodes:
        t = start_epoch + rng.randint(0, beacon_iv)
        while t < end_epoch:
            add(
                t,
                m["num"],
                BROADCAST,
                MESH_BEACON_APP,
                _pl_beacon(rng, chans),
                ch="LongFast",
                hop=3,
            )
            t += int(beacon_iv * rng.uniform(0.85, 1.15))

    packets.sort(key=lambda p: p[0])
    cap = Capture(
        nodes=node_rows, channels=chans, packets=packets, label=f"meshcon-{nodes}n-{days}d"
    )
    return cap


def fit_profile(capture, *, base: dict | None = None) -> dict:
    """Derive a sim PROFILE from a real capture, to make synthetic output match.

    Returns a dict mergeable into :data:`PROFILE` / passable as ``generate(
    profile=...)``: hardware + role mixes and channels from the node DB, a text
    rate and per-node POSITION/TELEMETRY intervals from observed traffic. Geo
    (venue/clusters) is left to the base profile. Pass the result straight to
    ``sim.generate(profile=fit_profile(cap))`` to synthesize a comparable mesh.
    """
    import itertools
    import statistics
    from collections import Counter, defaultdict

    prof = dict(base or PROFILE)
    nodes = capture.nodes
    # hardware / role mixes from the real node DB
    hw = Counter(n.hw_model for n in nodes if n.hw_model)
    role = Counter((n.role or "CLIENT") for n in nodes)
    if hw:
        prof["hw_weights"] = hw.most_common()
    if role:
        prof["role_weights"] = role.most_common()
    if capture.channels:
        prof["channels"] = list(capture.channels)

    # walk packets once: portnum mix + per-(node,portnum) timestamps
    times: dict[tuple[int, int], list[int]] = defaultdict(list)
    portnums: Counter = Counter()
    text_n = 0
    for rxt, raw, _ch in capture.packets:
        mp = mesh_pb2.MeshPacket()
        try:
            mp.ParseFromString(raw)
        except Exception:
            continue
        if mp.WhichOneof("payload_variant") != "decoded":
            continue
        pn = mp.decoded.portnum
        portnums[pn] += 1
        if pn == 1:
            text_n += 1
        if pn in (3, 67):
            times[(getattr(mp, "from"), pn)].append(rxt)

    span = capture.span
    hours = max((span[1] - span[0]) / 3600.0, 1e-6)
    n_nodes = max(len(nodes), 1)
    # text messages/hour normalised to the generator's 150-node baseline
    prof["text_base_msgs_per_hour"] = round(text_n / hours * (150.0 / n_nodes), 2)

    def _median_interval(portnum: int, default: int) -> int:
        deltas = []
        for (_node, pn), ts in times.items():
            if pn != portnum or len(ts) < 2:
                continue
            ts.sort()
            deltas += [b - a for a, b in itertools.pairwise(ts) if b > a]
        return int(statistics.median(deltas)) if deltas else default

    pos_iv = _median_interval(3, prof["pos_interval"]["default"])
    prof["pos_interval"] = {"mobile": max(pos_iv // 3, 30), "router": pos_iv * 2, "default": pos_iv}
    prof["telemetry_interval"] = _median_interval(67, prof["telemetry_interval"])
    prof["portnum_mix"] = dict(portnums.most_common())  # informational
    return prof


def _weighted_cluster(rng, pairs):
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0.0
    for c, w in pairs:
        upto += w
        if r <= upto:
            return c
    return pairs[-1][0]


def _pick_text(rng, ch, node_rows):
    # ~55% short one-liners (observed median ~18 chars), mostly themed lines,
    # plus a long tail of "wall of text" messages (real captures: p90 ~80,
    # max ~230-246 — chunked rants, pasted scripts). Long texts are composed
    # from the synthetic pools only.
    r = rng.random()
    pool = _CHATTER.get(ch, _CHATTER["LongFast"])
    if r < 0.55:
        return rng.choice(_SHORT)
    if r < 0.95:
        t = rng.choice(pool)
    else:
        t = " ".join(rng.choice(pool) for _ in range(rng.randint(2, 4)))
    if "{h}" in t:
        t = t.replace("{h}", rng.choice(node_rows).long_name)
    return t[:236]


# ── payload builders ─────────────────────────────────────────────────────────
# Value distributions below mirror the aggregate telemetry/position stats from
# the real Burning Man + DEF CON captures (battery mostly "plugged" 101, low
# air-util, a fat channel-utilisation tail, position precision clustering at
# 32/13/14/17 bits). Synthetic values only.
_PRECISION_WEIGHTS = [
    ("32", 34),
    ("13", 29),
    ("17", 3),
    ("0", 2),
    ("15", 2),
    ("14", 1),
    ("19", 1),
    ("12", 1),
]


def _chutil(rng: random.Random) -> float:
    # skewed low with a modest high tail (BM observed p50 ~7, p90 ~23, max ~39)
    r = rng.random()
    if r < 0.7:
        return round(rng.uniform(1, 15), 3)
    if r < 0.95:
        return round(rng.uniform(15, 28), 3)
    return round(rng.uniform(28, 45), 3)


def _pl_position(rng, m, t):
    p = mesh_pb2.Position()
    p.latitude_i = m["lat_i"] + (rng.randint(-3000, 3000) if m["mobile"] else 0)
    p.longitude_i = m["lon_i"] + (rng.randint(-3000, 3000) if m["mobile"] else 0)
    p.altitude = rng.randint(2100, 2400)
    p.time = t
    p.sats_in_view = rng.randint(5, 11)
    p.precision_bits = int(_weighted(rng, _PRECISION_WEIGHTS))
    p.PDOP = rng.randint(90, 250)
    return p.SerializeToString()


def _pl_nodeinfo(nr: NodeRow):
    u = mesh_pb2.User()
    u.id = nr.node_id
    u.long_name = nr.long_name or nr.node_id
    u.short_name = nr.short_name or nr.node_id[-4:]
    u.hw_model = _enum(mesh_pb2.HardwareModel, nr.hw_model)
    u.role = _enum(config_pb2.Config.DeviceConfig.Role, nr.role)
    return u.SerializeToString()


def _pl_tel_device(rng, t):
    tm = telemetry_pb2.Telemetry()
    tm.time = t
    d = tm.device_metrics
    # ~half of nodes report 101 (plugged in / charging); the rest spread 30-99
    d.battery_level = 101 if rng.random() < 0.45 else rng.randint(30, 99)
    d.voltage = (
        0.0 if d.battery_level == 101 and rng.random() < 0.3 else round(rng.uniform(3.5, 4.2), 3)
    )
    d.channel_utilization = _chutil(rng)
    # air-util skews very low; occasional busy node
    d.air_util_tx = round(
        rng.uniform(0.0, 0.2) if rng.random() < 0.85 else rng.uniform(0.2, 6.0), 4
    )
    d.uptime_seconds = rng.randint(60, 400000)
    return tm.SerializeToString()


def _pl_tel_env(rng, t):
    tm = telemetry_pb2.Telemetry()
    tm.time = t
    e = tm.environment_metrics
    e.temperature = round(rng.uniform(12, 33), 2)
    e.relative_humidity = round(rng.uniform(8, 40), 2)
    e.barometric_pressure = round(rng.uniform(770, 790), 2)
    e.voltage = round(rng.uniform(3.7, 4.2), 3)
    return tm.SerializeToString()


def _pl_tel_power(rng, t):
    tm = telemetry_pb2.Telemetry()
    tm.time = t
    p = tm.power_metrics
    p.ch1_voltage = round(rng.uniform(3.7, 4.2), 3)
    p.ch1_current = round(rng.uniform(20, 300), 2)
    return tm.SerializeToString()


def _pl_routing_ack():
    r = mesh_pb2.Routing()
    r.error_reason = mesh_pb2.Routing.Error.NONE
    return r.SerializeToString()


def _pl_traceroute(rng, route):
    rd = mesh_pb2.RouteDiscovery()
    for nn in route:
        rd.route.append(nn & 0xFFFFFFFF)
    for _ in route:
        rd.snr_towards.append(rng.randint(-120, 40))
    return rd.SerializeToString()


def _pl_neighborinfo(rng, num, neighbors):
    ni = mesh_pb2.NeighborInfo()
    ni.node_id = num
    ni.last_sent_by_id = num
    ni.node_broadcast_interval_secs = 14400
    for nb in neighbors:
        e = ni.neighbors.add()
        e.node_id = nb["num"]
        e.snr = round(rng.uniform(-18, 12), 1)
    return ni.SerializeToString()


def _pl_waypoint(rng, m, t, name, desc, icon, *, geofenced: bool = False):
    from .build import append_fields

    w = mesh_pb2.Waypoint()
    w.id = rng.randint(1, 0x7FFFFFFF)
    w.latitude_i = m["lat_i"] + rng.randint(-2000, 2000)
    w.longitude_i = m["lon_i"] + rng.randint(-2000, 2000)
    w.expire = t + rng.randint(3600, 86400)
    w.name = name
    w.description = desc
    w.icon = icon
    base = w.SerializeToString()
    if geofenced:
        radius = rng.randint(50, 500)
        base += append_fields({9: radius, 11: True, 12: True})
    return base


def _pl_pax(rng):
    px = paxcount_pb2.Paxcount()
    px.wifi = rng.randint(5, 250)
    px.ble = rng.randint(5, 400)
    px.uptime = rng.randint(60, 200000)
    return px.SerializeToString()


# Synthetic beacon messages for MESH_BEACON_APP packets — short, conference-
# flavoured texts that a beaconing node would broadcast to advertise its mesh.
_BEACON_MESSAGES = [
    "MeshCon mesh — join us!",
    "VLA mesh active — connect on LongFast",
    "Mesh beacon — conference channel available",
    "Open mesh at base camp",
    "Join the MeshCon network",
]


def _pl_beacon(rng: random.Random, chans: list[str]) -> bytes:
    """A synthetic MeshBeacon payload (MESH_BEACON_APP = 37)."""
    msg = rng.choice(_BEACON_MESSAGES)
    # Optionally advertise one of the conference channels
    offer_name = rng.choice(chans) if rng.random() < 0.6 else ""
    offer_psk = b"\x01" if offer_name == "LongFast" else b""
    return beacon_payload(
        msg,
        offer_channel_name=offer_name,
        offer_channel_psk=offer_psk,
        offer_region="US",
        offer_preset="LONG_FAST" if rng.random() < 0.7 else "",
    )


def _pl_sf():
    sf = storeforward_pb2.StoreAndForward()
    sf.rr = storeforward_pb2.StoreAndForward.RequestResponse.ROUTER_HEARTBEAT
    sf.heartbeat.period = 900
    sf.heartbeat.secondary = 0
    return sf.SerializeToString()


def _pl_admin():
    a = admin_pb2.AdminMessage()
    a.get_device_metadata_request = True
    return a.SerializeToString()
