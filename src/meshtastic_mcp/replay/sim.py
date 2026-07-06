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
    atak_pb2,
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
    "label_prefix": "meshcon",
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
    # Where text lands, by channel (name, weight). Names absent from the active
    # channel lineup collapse to LongFast; presets override this to match their
    # own channel names.
    "text_channel_weights": [
        ("LongFast", 34),
        ("MeshCon", 10),
        ("Talks", 16),
        ("Swap", 14),
        ("Hax", 14),
        ("Staff", 6),
    ],
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
    # Scripted traffic spikes (keynote, emergency): each entry multiplies the
    # text budget inside [start_h, start_h + hours) from the capture start.
    "spikes": [],
    # Venue climate for environment telemetry: diurnal temperature sinusoid
    # (peak mid-afternoon), humidity anti-correlated, pressure around the
    # venue-altitude mode. BM measured 8..55 C, humidity with real NaNs.
    "climate": {"t_mean": 22.0, "t_amp": 9.0, "pressure_hpa": 780.0, "nan_fraction": 0.02},
    # Battery population: fraction running on wall/solar power (reports 101);
    # the rest discharge at a per-node %/hour rate and bottom out at 0
    # (real captures show both a 101 mode and a 0 spike). Infra is always
    # plugged and emits disproportionately, so the client fraction stays low.
    "plugged_fraction": 0.12,
    # ATAK squad (opt-in): when team_nodes > 0, a squad emits TAKPacket PLI +
    # GeoChat + status (portnum 72). Off by default — no TAK traffic appeared
    # in the real captures. See WS-A in docs/sim-realism-plan.md.
    # ``wire``: "v1" (default) emits the legacy uncompressed TAKPacket; "v2"
    # emits real zstd-dictionary-compressed TAKPacketV2 payloads and requires
    # the [tak] extra (meshtastic-tak SDK) — see replay/tak.py.
    "tak": {
        "team_nodes": 0,
        "pli_interval": 45,
        "chat_per_hour": 2.0,
        "team": "Cyan",
        "channel": "LongFast",
        "wire": "v1",
    },
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
    # Observer / RF gateway model (see replay/observer.py): when enabled, the
    # generated "all traffic that exists" stream is filtered + duplicated into
    # what a single gateway at the venue would have heard — RF loss with
    # distance, rebroadcast copies with decremented hop_limit and their own
    # SNR/RSSI, relay_node stamps, optional via_mqtt bridging. Keys mirror
    # ObserverParams (lat/lon/seed default to the venue/generate seed).
    "observer": {"enabled": False},
}

# ── Scenario presets ─────────────────────────────────────────────────────────
# Partial PROFILE overrides for whole-event scenarios, deep-merged over the
# defaults. Tunables are reviewed constants informed by the real captures
# (Burning Man 2025, DEF CON 33) — no dataset files are shipped. Geo uses public
# venue coordinates; channel names are the events' published public channels.
PRESETS: dict[str, dict] = {
    # The default synthetic conference (VLA / MeshCon) — PROFILE as-is.
    "meshcon": {},
    # Burning Man: Black Rock City playa. Sparse/open terrain (high path-loss
    # exponent), mild talker skew, a 10-day arc, the Aug-26 flood emergency +
    # subsequent shitposting spike. Observer at centre camp.
    "burningman": {
        "label_prefix": "burningman",
        "venue": {"name": "Black Rock City, NV", "lat": 40.7864, "lon": -119.2065},
        "clusters": [
            ("Center Camp", 40.7864, -119.2065, 0.6, 40),
            ("2 o'clock", 40.7920, -119.2000, 0.8, 12),
            ("10 o'clock", 40.7930, -119.2140, 0.8, 12),
            ("4 o'clock", 40.7800, -119.1980, 0.8, 11),
            ("8 o'clock", 40.7800, -119.2150, 0.8, 11),
            ("Deep Playa", 40.8000, -119.2060, 2.5, 8),
            ("The Man", 40.7864, -119.2065, 0.3, 4),
            ("Gate/Greeters", 40.7550, -119.2330, 1.0, 2),
        ],
        "channels": ["LongFast", "Everyone", "BRC", "Playa", "Rangers"],
        "text_channel_weights": [
            ("LongFast", 30),
            ("Everyone", 34),
            ("BRC", 14),
            ("Playa", 14),
            ("Rangers", 8),
        ],
        "encrypted_fraction": 0.15,
        "climate": {"t_mean": 26.0, "t_amp": 16.0, "pressure_hpa": 858.0, "nan_fraction": 0.03},
        "observer": {
            "enabled": True,
            "path_loss_exp": 3.1,
            "sigma_db": 9.0,
            "loss_floor": 0.45,
            "mqtt_fraction": 0.03,
            "fade_good_s": 240.0,
            "fade_bad_s": 90.0,
        },
        # day-2 evening: dust-storm flood emergency, then late-night shitposting
        "spikes": [
            {"start_h": 42, "hours": 3, "text_x": 6.0},
            {"start_h": 45, "hours": 4, "text_x": 2.5},
        ],
    },
    # DEF CON: dense indoor convention. Lower path-loss but heavy collisions
    # (high loss floor), ~45% foreign/encrypted, ~40% via MQTT bridge, a
    # ShortTurbo-flavoured second plane, con-hours envelope, hop-7 crankers.
    "defcon": {
        "label_prefix": "defcon",
        "venue": {"name": "Las Vegas Convention Center", "lat": 36.1312, "lon": -115.1516},
        "clusters": [
            ("Contest Area", 36.1312, -115.1516, 0.15, 44),
            ("Village Halls", 36.1330, -115.1500, 0.20, 22),
            ("Talks", 36.1300, -115.1530, 0.20, 14),
            ("Chillout", 36.1290, -115.1510, 0.20, 8),
            ("Vendor", 36.1320, -115.1490, 0.20, 6),
            ("Hotels", 36.1250, -115.1600, 1.2, 4),
            ("Hallway", 36.1312, -115.1516, 0.10, 2),
        ],
        "channels": ["LongFast", "DEFCONnect", "HackerComms", "NodeChat", "MeshCon"],
        "text_channel_weights": [
            ("LongFast", 20),
            ("DEFCONnect", 34),
            ("HackerComms", 20),
            ("NodeChat", 18),
            ("MeshCon", 8),
        ],
        "encrypted_fraction": 0.45,
        "hop_start_weights": [("3", 68), ("7", 18), ("4", 6), ("2", 5), ("5", 3)],
        "climate": {"t_mean": 22.0, "t_amp": 2.0, "pressure_hpa": 940.0, "nan_fraction": 0.02},
        "observer": {
            "enabled": True,
            "path_loss_exp": 2.6,
            "sigma_db": 8.0,
            "loss_floor": 0.62,
            "mqtt_fraction": 0.40,
            "fade_good_s": 150.0,
            "fade_bad_s": 70.0,
        },
        "spikes": [{"start_h": 33, "hours": 6, "text_x": 2.0}],
    },
}

# Config keys whose values are themselves dicts and should deep-merge (rather
# than wholesale-replace) when a profile override is applied.
_MERGE_DICT_KEYS = frozenset({"venue", "observer", "climate", "pos_interval", "tak"})


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k in _MERGE_DICT_KEYS and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _resolve_profile(profile: dict | str | None) -> dict:
    """Merge a profile override (dict / preset name / JSON path) over PROFILE."""
    if profile is None:
        return dict(PROFILE)
    if isinstance(profile, str):
        if profile in PRESETS:
            return _deep_merge(PROFILE, PRESETS[profile])
        # Non-preset strings are only ever treated as JSON files (e.g. a
        # fit_profile() dump) and must be an explicit .json path — this keeps
        # a mistyped preset name from silently opening some other file, and
        # gives a clear error. Note the MCP replay_start tool only ever passes
        # a validated preset name here, never caller-controlled paths.
        if not profile.endswith(".json"):
            raise ValueError(
                f"unknown profile {profile!r}: expected one of {sorted(PRESETS)} "
                "or a path to a .json profile file"
            )
        import json

        with open(profile) as fh:
            profile = json.load(fh)
    if not isinstance(profile, dict):
        raise TypeError(f"unsupported profile: {type(profile).__name__}")
    return _deep_merge(PROFILE, profile)


def preset_profile(name: str, override: dict | None = None) -> dict:
    """Resolve a preset name to a full profile dict, optionally deep-merging an
    override on top.

    The public, path-free entry point the ``replay_start`` MCP tool uses to
    expose scenario tunables (observer/tak/spikes/climate/…) without accepting
    a filesystem path from an untrusted caller. ``name`` must be a known preset
    (see :data:`PRESETS`); ``override`` is a partial profile dict merged with the
    same nested-dict rules as :func:`generate`'s ``profile=`` argument.
    """
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}: expected one of {sorted(PRESETS)}")
    base = _resolve_profile(name)
    return _deep_merge(base, override) if override else base


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
    pid,
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
    mp.id = pid & 0xFFFFFFFF
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


def _mp_enc(rng, pid, frm, hop_start, ch_hash) -> bytes:
    """An encrypted (undecodable) packet — models traffic on a channel/key the
    viewer lacks (DEF CON ran ~45% such 'foreign' traffic). ``ch_hash`` is the
    OTA channel-hash byte a real radio would report (not a settings index)."""
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", frm & 0xFFFFFFFF)
    mp.to = 0xFFFFFFFF
    mp.id = pid & 0xFFFFFFFF
    mp.hop_start = max(0, hop_start)
    mp.hop_limit = max(0, hop_start - _hops_taken(rng, hop_start))
    mp.channel = ch_hash & 0xFF
    base = int(_weighted(rng, _ENC_LEN_WEIGHTS))
    mp.encrypted = bytes(rng.randint(0, 255) for _ in range(max(16, base + rng.randint(-6, 6))))
    return mp.SerializeToString()


def _emit_encrypted(
    rng: random.Random,
    P: dict,
    packets: list[tuple[int, bytes, str]],
    pid_counter: itertools.count,
    *,
    nodes: int,
    start_epoch: int,
    end_epoch: int,
    hod,
) -> None:
    """Append encrypted/foreign traffic: packets on channels the viewer can't
    decode, from "heard but unknown" neighbour nodes (no NodeInfo). The count is
    sized so encrypted packets are ``encrypted_fraction`` of the *final* stream
    (DEF CON ran ~45%). Foreign traffic is largely independent of the event's
    diurnal rhythm. Mutates ``packets`` in place.
    """
    frac = P.get("encrypted_fraction", 0.0)
    if frac <= 0 or not packets:
        return
    n_enc = int(len(packets) * frac / max(1e-6, 1.0 - frac))
    foreign = [rng.randint(0x10000000, 0xEFFFFFFF) for _ in range(max(5, nodes // 8))]
    foreign_hs = {f: int(_weighted(rng, P["hop_start_weights"])) for f in foreign}
    # a handful of foreign channels, each with a realistic OTA hash byte
    foreign_hashes = [rng.randint(1, 255) for _ in range(rng.randint(2, 4))]
    added = attempts = 0
    while added < n_enc and attempts < n_enc * 10:
        attempts += 1
        t = rng.randint(start_epoch, end_epoch - 1)
        if rng.random() > _activity(hod(t)) * 0.4 + 0.6:
            continue
        f = rng.choice(foreign)
        packets.append(
            (
                t,
                _mp_enc(rng, next(pid_counter), f, foreign_hs[f], rng.choice(foreign_hashes)),
                "LongFast",
            )
        )
        added += 1


def _build_nodes(
    rng: random.Random, P: dict, *, nodes: int, days: int, start_epoch: int
) -> tuple[list[NodeRow], list[dict]]:
    """Construct the node DB: ``(node_rows, meta)``.

    The first 8 nodes are always infrastructure (4 ROUTER + 4 ROUTER_LATE) named
    from :data:`_ROUTER_NAMES`; the rest draw role/cluster/hardware from the
    profile weights. ``meta`` carries the per-node simulation state the emitters
    need (position, talkativeness, hop_start, battery persona, chutil gain, and
    presence window). All draws come from ``rng`` in a fixed order so the
    capture stays byte-for-byte reproducible.
    """
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
        # Battery persona: plugged nodes report 101; the rest discharge at a
        # per-node rate and bottom out at 0 (both modes appear in real data).
        if infra or rng.random() < P["plugged_fraction"]:
            batt = (101.0, 0.0)
        else:
            batt = (rng.uniform(40.0, 100.0), rng.uniform(2.0, 10.0))
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
                "batt": batt,
                # per-node chutil gain: what fraction of the mesh's airtime this
                # node's radio actually hears (distance/terrain dependent)
                "ch_gain": rng.uniform(0.1, 0.9),
                "join_t": start_epoch + int(join_h * 3600),
                "leave_t": start_epoch + int((join_h + stay_h) * 3600),
            }
        )
    return node_rows, meta


def generate(
    *,
    nodes: int = 800,
    days: int = 3,
    start: int | None = None,
    seed: int = 1337,
    channels: list[str] | None = None,
    text_scale: float = 1.0,
    profile: dict | str | None = None,
) -> Capture:
    """Generate a synthetic capture in memory.

    ``profile`` is a partial :data:`PROFILE` override: a dict, a path to a JSON
    file (e.g. one produced by :func:`fit_profile`), or a preset name from
    :data:`PRESETS` (``meshcon`` / ``burningman`` / ``defcon``). Nested config
    dicts (venue/observer/climate/pos_interval) deep-merge over the defaults.
    """
    P = _resolve_profile(profile)
    rng = random.Random(seed)
    chans = channels or list(P["channels"])
    ch_index = {c: i for i, c in enumerate(chans)}
    start_epoch = int(start if start is not None else time.time())
    end_epoch = start_epoch + days * 86400

    def hod(t):
        return ((t - start_epoch) / 3600.0) % 24

    node_rows, meta = _build_nodes(rng, P, nodes=nodes, days=days, start_epoch=start_epoch)

    packets: list[tuple[int, bytes, str]] = []
    routers = [m for m in meta if m["role"].startswith("ROUTER")]
    sensors = [m for m, nr in zip(meta, node_rows, strict=False) if nr.role == "SENSOR"]
    by_num = {nr.num: nr for nr in node_rows}
    hop_starts = {m["num"]: m["hop_start"] for m in meta}
    # packet-id counter is local so a given (seed, nodes, days, start) is
    # byte-for-byte reproducible across calls in the same process
    pid_counter = itertools.count(0x10000001)

    tel_pending: list[tuple[int, dict]] = []

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
                    next(pid_counter),
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
        # Talkativeness skews *social* traffic hard, but position beacons only
        # mildly, and telemetry not at all — telemetry cadence is a fixed
        # firmware default for every role (infra dominance in real captures
        # comes from persistent presence, not a faster clock).
        pos_iv = max(30, pos_iv / talk**0.35)
        tel_iv = P["telemetry_interval"]
        t = jt + rng.randint(0, 300)
        while t < node_end:
            if rng.random() < _activity(hod(t)) + 0.1:
                add(t, m["num"], BROADCAST, 3, _pl_position(rng, m, t))
            t += int(pos_iv * rng.uniform(0.8, 1.2))
        t = jt + rng.randint(0, 600)
        while t < node_end:
            if rng.random() < _activity(hod(t)) + 0.15:
                # deferred: chutil is derived from the *actual* generated
                # airtime, so device telemetry is emitted in a second pass
                tel_pending.append((t, m))
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
        # sensor persona: which fields this board reports, fixed per node
        persona = _weighted(rng, _ENV_PERSONAS)
        t = start_epoch + rng.randint(0, P["env_interval"])
        while t < end_epoch:
            add(t, m["num"], BROADCAST, 67, _pl_tel_env(rng, P["climate"], persona, hod(t), t))
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

    # -- text chatter: conversation bursts, not a uniform smear. A burst is a
    # few nodes trading messages ~45 s apart on one channel (real inter-arrival
    # is heavy-tailed: replies cluster within minutes, then silence). Scripted
    # spikes (keynote / emergency) multiply the hourly budget. --
    base = P["text_base_msgs_per_hour"] * (nodes / 150.0) * text_scale
    spikes = P.get("spikes") or []
    for hour in range(days * 24):
        t0 = start_epoch + hour * 3600
        mult = 1.0
        for sp in spikes:
            if sp["start_h"] <= hour < sp["start_h"] + sp["hours"]:
                mult *= sp.get("text_x", 1.0)
        budget = int(rng.gauss(base * _text_env(hod(t0)) * mult, base * 0.25))
        while budget > 0:
            ch = _weighted(rng, P["text_channel_weights"])
            if ch not in ch_index:
                ch = "LongFast"
            burst = min(budget, max(1, int(rng.lognormvariate(0.9, 0.8))))
            budget -= burst
            t = t0 + rng.randint(0, 3599)
            party = rng.sample(node_rows, min(len(node_rows), rng.randint(2, 4)))
            for _ in range(burst):
                sender = rng.choice(party)
                text = _pick_text(rng, ch, node_rows)
                dm = rng.random() < P["text_dm_fraction"]
                to = rng.choice(node_rows).num if dm else BROADCAST
                add(t, sender.num, to, 1, text.encode("utf-8"), ch=ch)
                if dm and rng.random() < P["ack_ratio"]:
                    add(t + rng.randint(1, 5), to, sender.num, 5, _pl_routing_ack(), ch=ch, hop=0)
                t += 2 + int(rng.expovariate(1 / 45.0))

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

    # -- ATAK squad (opt-in, off by default): a team of nodes emitting TAK PLI +
    # GeoChat + status, for exercising an app's TAK plane (the Meshtastic app's
    # in-app TAK server bridges these to ATAK/iTAK over CoT). Legacy v1 rides
    # ATAK_PLUGIN (72); v2 wire rides ATAK_PLUGIN_V2 (78). No TAK traffic
    # appeared in the real captures, so this is a scenario knob, not fitted. --
    tak_cfg = P.get("tak") or {}
    tak_n = int(tak_cfg.get("team_nodes", 0))
    if tak_n > 0 and meta:
        team_val = _enum(atak_pb2.Team, tak_cfg.get("team", "Cyan"), 0)
        tak_ch = tak_cfg.get("channel", "LongFast")
        if tak_ch not in ch_index:
            tak_ch = chans[0]
        pli_iv = int(tak_cfg.get("pli_interval", 45))
        chat_iv = 3600.0 / max(float(tak_cfg.get("chat_per_hour", 2.0)), 0.01)
        v2_wire = tak_cfg.get("wire", "v1") == "v2"
        # Firmware >= 2.8 carries compressed TAKPacketV2 on ATAK_PLUGIN_V2 (78);
        # legacy uncompressed TAKPacket stays on ATAK_PLUGIN (72).
        tak_port = 78 if v2_wire else 72
        if v2_wire:
            from . import tak as _tak

            _tak._require()  # fail fast with an install hint if the extra is absent
        squad = rng.sample(meta, min(len(meta), tak_n))
        for i, m in enumerate(squad):
            role_name = "TeamLead" if i == 0 else "Medic" if i == 1 else "TeamMember"
            role_val = _enum(atak_pb2.MemberRole, role_name, 1)
            callsign = f"{rng.choice(_ADJ)}-{i + 1}"
            uid = f"MESH-{m['num']:08x}"
            node_end = min(end_epoch, m["leave_t"])
            t = m["join_t"] + rng.randint(0, pli_iv)
            while t < node_end:
                lat_i = m["lat_i"] + rng.randint(-4000, 4000)
                lon_i = m["lon_i"] + rng.randint(-4000, 4000)
                batt_lvl = _batt_level(m, t)
                if v2_wire:
                    pkt = _tak.build_pli(
                        callsign=callsign,
                        uid=uid,
                        team=team_val,
                        role=role_val,
                        lat_i=lat_i,
                        lon_i=lon_i,
                        altitude=rng.randint(2000, 2500),
                        speed=rng.randint(0, 8),
                        course=rng.randint(0, 359),
                        battery=min(100, batt_lvl),
                    )
                    pl = _tak.compress(pkt)
                else:
                    pl = _pl_tak_pli(rng, callsign, team_val, role_val, lat_i, lon_i, batt_lvl)
                add(t, m["num"], BROADCAST, tak_port, pl, ch=tak_ch)
                t += int(pli_iv * rng.uniform(0.8, 1.2))
            t = m["join_t"] + rng.randint(0, int(chat_iv))
            while t < node_end:
                if rng.random() < _activity(hod(t)) + 0.2:
                    batt_lvl = _batt_level(m, t)
                    if v2_wire:
                        pkt = _tak.build_chat(
                            callsign=callsign,
                            uid=uid,
                            team=team_val,
                            role=role_val,
                            battery=min(100, batt_lvl),
                            message=rng.choice(_TAK_CHAT),
                        )
                        pl = _tak.compress(pkt)
                    else:
                        pl = _pl_tak_chat(rng, callsign, team_val, role_val, batt_lvl)
                    add(t, m["num"], BROADCAST, tak_port, pl, ch=tak_ch)
                t += int(chat_iv * rng.uniform(0.7, 1.3))

    # -- device telemetry second pass: chutil tracks the actual per-hour
    # generated load (real chutil follows the diurnal traffic envelope) --
    hour_hist: dict[int, int] = {}
    for t, _raw, _ch in packets:
        hour_hist[(t - start_epoch) // 3600] = hour_hist.get((t - start_epoch) // 3600, 0) + 1
    peak_rate = max(hour_hist.values()) if hour_hist else 1
    for t, m in tel_pending:
        load = hour_hist.get((t - start_epoch) // 3600, 0) / peak_rate
        add(t, m["num"], BROADCAST, 67, _pl_tel_device(rng, m, t, load))

    _emit_encrypted(
        rng,
        P,
        packets,
        pid_counter,
        nodes=nodes,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        hod=hod,
    )

    packets.sort(key=lambda p: p[0])

    # -- observer stage: collapse the omniscient stream into a gateway view --
    obs_cfg = dict(P.get("observer") or {})
    if obs_cfg.pop("enabled", False):
        from .observer import ObserverParams, observe

        obs_cfg.setdefault("lat", P["venue"]["lat"])
        obs_cfg.setdefault("lon", P["venue"]["lon"])
        obs_cfg.setdefault("seed", seed)
        if "dup_weights" in obs_cfg:
            obs_cfg["dup_weights"] = tuple(tuple(x) for x in obs_cfg["dup_weights"])
        positions = {m["num"]: (m["lat_i"] / 1e7, m["lon_i"] / 1e7) for m in meta}
        packets = observe(packets, positions, ObserverParams(**obs_cfg))

    cap = Capture(
        nodes=node_rows,
        channels=chans,
        packets=packets,
        label=f"{P.get('label_prefix', 'meshcon')}-{nodes}n-{days}d",
    )
    return cap


def fit_profile(capture, *, base: dict | None = None) -> dict:
    """Derive a sim PROFILE from a real capture, to make synthetic output match.

    Returns a dict mergeable into :data:`PROFILE` / passable as ``generate(
    profile=...)``: hardware + role mixes and channels from the node DB, text
    rate + DM fraction, per-node POSITION/TELEMETRY intervals, hop_start
    distribution, encrypted fraction, and the channel-hash text weighting, all
    derived from observed traffic. Geo (venue/clusters/climate) is left to the
    base profile. Pass the result straight to
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

    # walk packets once: portnum mix, hop starts, per-(node,portnum) times,
    # per-channel text volume, DM fraction, encrypted fraction
    times: dict[tuple[int, int], list[int]] = defaultdict(list)
    portnums: Counter = Counter()
    hop_starts: Counter = Counter()
    text_by_ch: Counter = Counter()
    text_n = text_dm = total = encrypted = 0
    for rxt, raw, ch in capture.packets:
        mp = mesh_pb2.MeshPacket()
        try:
            mp.ParseFromString(raw)
        except Exception:
            continue
        total += 1
        if mp.hop_start:
            hop_starts[mp.hop_start] += 1
        if mp.WhichOneof("payload_variant") != "decoded":
            encrypted += 1
            continue
        pn = mp.decoded.portnum
        portnums[pn] += 1
        if pn == 1:
            text_n += 1
            text_by_ch[ch] += 1
            if mp.to != BROADCAST:
                text_dm += 1
        if pn in (3, 67):
            times[(getattr(mp, "from"), pn)].append(rxt)

    span = capture.span
    hours = max((span[1] - span[0]) / 3600.0, 1e-6)
    n_nodes = max(len(nodes), 1)
    # text messages/hour normalised to the generator's 150-node baseline
    prof["text_base_msgs_per_hour"] = round(text_n / hours * (150.0 / n_nodes), 2)
    if text_n:
        prof["text_dm_fraction"] = round(text_dm / text_n, 3)
    if total:
        prof["encrypted_fraction"] = round(encrypted / total, 3)
    if hop_starts:
        prof["hop_start_weights"] = [(str(h), c) for h, c in hop_starts.most_common()]
    if text_by_ch:
        prof["text_channel_weights"] = [(ch, c) for ch, c in text_by_ch.most_common()]

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


# Env-sensor personas: which fields a given board reports, with weights fit to
# the observed field-presence ratios (BM: temperature 100%, lux 63%, humidity
# 36%, pressure 17%, gas/IAQ 3.4%). T=temp L=lux H=humidity P=pressure G=gas+IAQ.
_ENV_PERSONAS = [
    ("TL", 40),
    ("T", 20),
    ("TLH", 15),
    ("THP", 12),
    ("TLHP", 8),
    ("THPG", 4),
]


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


def _batt_level(m, t) -> int:
    """Battery % for node ``m`` at time ``t``: 101 if plugged, else a linear
    discharge from its start level bottoming out at 0."""
    start, rate = m["batt"]
    if start >= 101:
        return 101
    hours = max(0.0, (t - m["join_t"]) / 3600.0)
    return max(0, int(start - rate * hours))


def _pl_tak_pli(rng, callsign, team_val, role_val, lat_i, lon_i, batt) -> bytes:
    """A TAKPacket (portnum 72) position/location-information report."""
    tp = atak_pb2.TAKPacket()
    tp.is_compressed = False
    tp.contact.callsign = callsign
    tp.contact.device_callsign = callsign
    tp.group.team = team_val
    tp.group.role = role_val
    tp.status.battery = min(100, batt)
    p = tp.pli
    p.latitude_i = lat_i
    p.longitude_i = lon_i
    p.altitude = rng.randint(2000, 2500)
    p.speed = rng.randint(0, 8)
    p.course = rng.randint(0, 359)
    return tp.SerializeToString()


_TAK_CHAT = [
    "moving to OP",
    "eyes on objective",
    "hold position",
    "copy all",
    "rally at CP1",
    "requesting sitrep",
    "green light",
    "RTB",
    "checkpoint clear",
    "need a medic",
]


def _pl_tak_chat(rng, callsign, team_val, role_val, batt) -> bytes:
    """A TAKPacket (portnum 72) GeoChat message to the team room."""
    tp = atak_pb2.TAKPacket()
    tp.is_compressed = False
    tp.contact.callsign = callsign
    tp.contact.device_callsign = callsign
    tp.group.team = team_val
    tp.group.role = role_val
    tp.status.battery = min(100, batt)
    tp.chat.message = rng.choice(_TAK_CHAT)
    tp.chat.to = "All Chat Rooms"
    return tp.SerializeToString()


def _pl_tel_device(rng, m, t, load):
    """Device metrics with per-node battery state and load-derived chutil.

    ``load`` is this hour's generated traffic normalised to the busiest hour,
    so channel utilisation tracks the actual diurnal envelope (BM observed
    p50 ~7, p90 ~23, max ~39). Battery follows the node's persona: plugged
    nodes report 101; discharging nodes drain linearly and bottom out at 0.
    """
    tm = telemetry_pb2.Telemetry()
    tm.time = t
    d = tm.device_metrics
    lvl = _batt_level(m, t)
    d.battery_level = lvl
    if lvl >= 101:
        d.voltage = 0.0 if rng.random() < 0.4 else round(rng.uniform(3.9, 4.25), 3)
    else:
        d.voltage = round(3.0 + 1.25 * lvl / 100.0 + rng.uniform(-0.05, 0.05), 3)
    gain = m.get("ch_gain", 0.5)
    d.channel_utilization = round(
        min(60.0, 0.8 + 36.0 * load**1.6 * gain * rng.uniform(0.8, 1.2)), 3
    )
    # air-util skews very low; occasional busy node
    d.air_util_tx = round(
        rng.uniform(0.0, 0.2) if rng.random() < 0.85 else rng.uniform(0.2, 6.0), 4
    )
    d.uptime_seconds = max(60, t - m["join_t"] + rng.randint(0, 900))
    return tm.SerializeToString()


def _pl_tel_env(rng, climate, persona, hod_f, t):
    """Environment metrics from the venue climate model + the node's persona.

    Temperature is a diurnal sinusoid (peak mid-afternoon), humidity is
    anti-correlated with temperature and occasionally NaN (real sensors emit
    NaN — clients must cope), lux follows solar elevation, pressure random-
    walks around the venue-altitude mode.
    """
    tm = telemetry_pb2.Telemetry()
    tm.time = t
    e = tm.environment_metrics
    temp = (
        climate["t_mean"]
        + climate["t_amp"] * math.cos(2.0 * math.pi * (hod_f - 15.0) / 24.0)
        + rng.gauss(0.0, 0.8)
    )
    e.temperature = round(temp, 2)
    if "H" in persona:
        if rng.random() < climate.get("nan_fraction", 0.0):
            e.relative_humidity = float("nan")
        else:
            hum = 75.0 - 1.8 * (temp - 10.0) + rng.gauss(0.0, 6.0)
            e.relative_humidity = round(min(96.0, max(4.0, hum)), 2)
    if "P" in persona:
        e.barometric_pressure = round(climate["pressure_hpa"] + rng.gauss(0.0, 2.5), 2)
    if "L" in persona:
        sun = math.sin(math.pi * (hod_f - 6.0) / 12.0) if 6.0 <= hod_f <= 18.0 else 0.0
        e.lux = round(max(0.0, sun) * rng.uniform(20000.0, 90000.0) + rng.uniform(0.0, 40.0), 2)
    if "G" in persona:
        e.gas_resistance = round(rng.uniform(10000.0, 200000.0), 1)
        e.iaq = rng.randint(10, 150)
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
