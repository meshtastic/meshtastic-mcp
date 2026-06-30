# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Hardware-free tests for the replay engine + synthetic mesh generator.

Covers the three things the replay surface promises:
  1. `sim.generate` produces a seeded, PII-free capture with every portnum.
  2. A `ReplaySession` serves it over TCP: the want-config handshake yields
     my_info + the node DB + channels + config, then a paced packet stream.
  3. The SQLite round-trip (`sim` -> DB -> `from_sqlite`) is loss-free for the
     fields the engine streams (the path DEF CON / Burning Man captures use).
"""

from __future__ import annotations

import socket
import sqlite3
import struct
import time
from collections import Counter

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import ReplayParams, ReplaySession, capture, fuzz, sim

ALL_PORTNUMS = {1, 3, 4, 5, 6, 8, 34, 65, 66, 67, 70, 71}


def _portnum_counts(cap) -> Counter:
    counts: Counter = Counter()
    for _ts, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        counts[mp.decoded.portnum] += 1
    return counts


def test_sim_is_seeded_and_has_full_portnum_breadth():
    cap_a = sim.generate(nodes=60, days=2, seed=7, start=1_700_000_000)
    cap_b = sim.generate(nodes=60, days=2, seed=7, start=1_700_000_000)
    # deterministic per seed
    assert [p[0] for p in cap_a.packets] == [p[0] for p in cap_b.packets]
    assert len(cap_a.packets) == len(cap_b.packets)
    # breadth: every Meshtastic portnum/flavor present
    counts = _portnum_counts(cap_a)
    assert ALL_PORTNUMS.issubset(set(counts)), f"missing portnums: {ALL_PORTNUMS - set(counts)}"
    # packets are time-ordered
    ts = [p[0] for p in cap_a.packets]
    assert ts == sorted(ts)
    assert 66 in counts  # RANGE_TEST present (DEF-CON-informed)
    # channels include the themed lineup
    assert cap_a.channels[0] == "LongFast"
    assert "MeshCon" in cap_a.channels


def _read_frame(sock: socket.socket) -> mesh_pb2.FromRadio:
    state = 0
    while True:
        x = sock.recv(1)[0]
        if state == 0 and x == 0x94:
            state = 1
        elif state == 1 and x == 0xC3:
            break
        else:
            state = 1 if x == 0x94 else 0
    (length,) = struct.unpack(">H", _exact(sock, 2))
    fr = mesh_pb2.FromRadio()
    fr.ParseFromString(_exact(sock, length))
    return fr


def _exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        buf += sock.recv(n - len(buf))
    return buf


def _send_toradio(sock: socket.socket, **kw) -> None:
    payload = mesh_pb2.ToRadio(**kw).SerializeToString()
    sock.sendall(bytes([0x94, 0xC3]) + struct.pack(">H", len(payload)) + payload)


def test_session_handshake_and_stream():
    cap = sim.generate(nodes=40, days=1, seed=11, start=1_700_000_000)
    params = ReplayParams(host="127.0.0.1", port=0, rate=500, node_delay=0)
    # bind an ephemeral port by letting the OS choose, then read it back
    sess = ReplaySession("test", cap, params)
    # claim a free port deterministically
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess.params.port = port
    sess.start()
    try:
        deadline = time.time() + 5
        client = None
        while time.time() < deadline:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        assert client is not None, "could not connect to replay session"

        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)

        variants: Counter = Counter()
        my_node = None
        node_infos = 0
        packets = 0
        t0 = time.time()
        while time.time() - t0 < 3 and packets < 50:
            fr = _read_frame(client)
            v = fr.WhichOneof("payload_variant")
            variants[v] += 1
            if v == "my_info":
                my_node = fr.my_info.my_node_num
            elif v == "node_info":
                node_infos += 1
            elif v == "packet":
                packets += 1
        client.close()

        assert variants["my_info"] == 1
        assert my_node is not None
        # observer + all generated nodes streamed during the DB phase
        assert node_infos >= len(cap.nodes)
        assert variants["channel"] == len(cap.channels)
        assert packets >= 1
        assert sess.state.packets_sent >= packets
    finally:
        sess.stop()


def test_get_owner_request_is_answered_for_strict_clients():
    """A get_owner_request during seeding gets an owner+passkey response.

    Strict clients (e.g. the Kotlin SDK) block their post-NodeDB "seeding" step
    on this admin round-trip; the replay device must emulate the firmware reply
    so they can reach a ready state.
    """
    from meshtastic.protobuf import admin_pb2, portnums_pb2

    from meshtastic_mcp.replay.engine import OBSERVER_NUM

    cap = sim.generate(nodes=8, days=1, seed=5)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess = ReplaySession("owner", cap, ReplayParams(host="127.0.0.1", port=port, node_delay=0))
    sess.start()
    try:
        deadline = time.time() + 5
        client = None
        while time.time() < deadline:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        assert client is not None

        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)

        # Send the admin get_owner_request the SDK issues to seed the passkey.
        req = admin_pb2.AdminMessage(get_owner_request=True)
        mp = mesh_pb2.MeshPacket()
        mp.to = OBSERVER_NUM
        mp.id = 0xABCDEF
        mp.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        mp.decoded.payload = req.SerializeToString()
        _send_toradio(client, packet=mp)

        owner_resp = None
        t0 = time.time()
        while time.time() - t0 < 4 and owner_resp is None:
            fr = _read_frame(client)
            if fr.WhichOneof("payload_variant") != "packet":
                continue
            d = fr.packet.decoded
            if d.portnum != portnums_pb2.PortNum.ADMIN_APP:
                continue
            am = admin_pb2.AdminMessage()
            am.ParseFromString(d.payload)
            if am.WhichOneof("payload_variant") == "get_owner_response":
                owner_resp = (fr.packet, am)
        client.close()

        assert owner_resp is not None, "no get_owner_response from the replay device"
        pkt, am = owner_resp
        assert getattr(pkt, "from") == OBSERVER_NUM  # client keys the passkey on this
        assert len(am.session_passkey) > 0
        assert am.get_owner_response.id == f"!{OBSERVER_NUM:08x}"
        assert pkt.decoded.request_id == 0xABCDEF  # echoes the request id
    finally:
        sess.stop()


def test_sqlite_roundtrip_is_lossless(tmp_path):
    cap = sim.generate(nodes=30, days=1, seed=3, start=1_700_000_000)
    db = tmp_path / "rt.db"
    _write_sqlite(db, cap)
    loaded = capture.from_sqlite(db, limit_nodes=0)

    assert len(loaded.nodes) == len(cap.nodes)
    assert len(loaded.packets) == len(cap.packets)
    # channels with no traffic correctly don't appear in the reloaded DB
    assert set(loaded.channels).issubset(set(cap.channels))
    assert "LongFast" in loaded.channels
    # payload bytes survive the round-trip exactly
    orig = sorted(p[1] for p in cap.packets)
    back = sorted(p[1] for p in loaded.packets)
    assert orig == back


def _write_sqlite(path, cap) -> None:
    """Persist a Capture into the BM/DEF CON/MeshCon schema for round-trip tests."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE node (id VARCHAR PRIMARY KEY, node_id BIGINT UNIQUE, long_name VARCHAR,
            short_name VARCHAR, hw_model VARCHAR, firmware VARCHAR, role VARCHAR,
            last_lat BIGINT, last_long BIGINT, channel VARCHAR, last_update DATETIME);
        CREATE TABLE packet (id BIGINT PRIMARY KEY, portnum INTEGER, from_node_id BIGINT,
            to_node_id BIGINT, payload BLOB, import_time DATETIME, channel VARCHAR);
        CREATE TABLE packet_seen (packet_id BIGINT, node_id BIGINT, rx_time BIGINT,
            hop_limit INTEGER, hop_start INTEGER, channel VARCHAR, rx_snr FLOAT,
            rx_rssi INTEGER, topic VARCHAR, import_time DATETIME,
            PRIMARY KEY (packet_id, node_id, rx_time));
        """
    )
    for i, n in enumerate(cap.nodes):
        conn.execute(
            "INSERT INTO node VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                n.node_id,
                n.num,
                n.long_name,
                n.short_name,
                n.hw_model,
                "2.7.8",
                n.role,
                n.lat_i,
                n.lon_i,
                "LongFast",
                2_000_000_000 - i,
            ),
        )
    for pid, (ts, raw, ch) in enumerate(cap.packets, start=1):
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        conn.execute(
            "INSERT INTO packet VALUES (?,?,?,?,?,?,?)",
            (pid, mp.decoded.portnum, getattr(mp, "from"), mp.to, raw, "x", ch),
        )
        conn.execute(
            "INSERT INTO packet_seen VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, getattr(mp, "from"), ts, mp.hop_limit, mp.hop_start, ch, 6.0, -50, "t", "x"),
        )
    conn.commit()
    conn.close()


def test_all_sim_data_is_synthetic():
    # The sim is informed by *aggregate stats* from real captures, but every
    # identity/position/message must be generated. Guard the PII vectors.
    from meshtastic_mcp.replay import sim as _sim

    cap = _sim.generate(nodes=80, days=1, seed=5, start=1_700_000_000)
    router_names = set(_sim._ROUTER_NAMES)
    for n in cap.nodes:
        # node ids are synthetic !hex; names are router names or "<Adj> <Noun>"
        assert n.node_id.startswith("!")
        if n.long_name in router_names:
            continue
        adj, _, noun = n.long_name.partition(" ")
        assert adj in _sim._ADJ and noun in _sim._NOUN, n.long_name
    # positions sit in the synthetic VLA venue, not any real capture coordinates
    for n in cap.nodes:
        if n.lat_i:
            assert 33_000_000_0 < n.lat_i < 35_000_000_0
            assert -109_000_000_0 < n.lon_i < -106_000_000_0
    # text payloads come only from the synthetic CHATTER pools (or range-test seq)
    pool = {t for msgs in _sim._CHATTER.values() for t in msgs}
    templates = [p.split("{h}")[0] for p in pool]
    for _ts, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.decoded.portnum == 1:
            txt = mp.decoded.payload.decode("utf-8", "replace")
            assert any(txt.startswith(t) for t in templates), txt


# ── Fuzzer ───────────────────────────────────────────────────────────────────
def test_fuzz_from_spec_resolves_presets_and_overrides():
    assert fuzz.from_spec(None) is None
    assert fuzz.from_spec("off") is None  # off == disabled
    cfg = fuzz.from_spec("parser", seed=3)
    assert cfg is not None and cfg.name == "parser" and cfg.corrupt_payload > 0
    over = fuzz.from_spec({"preset": "adversary", "flooder_rate": 20, "seed": 9})
    assert over is not None and over.flooder is True and over.flooder_rate == 20 and over.seed == 9
    with pytest.raises(ValueError):
        fuzz.from_spec("nope")


def test_fuzz_on_packet_mutates_and_stays_serializable():
    cap = sim.generate(nodes=20, days=1, seed=1, start=1_700_000_000)
    chi = {c: i for i, c in enumerate(cap.channels)}
    fz = fuzz.Fuzzer(fuzz.preset("chaos", seed=4), cap.nodes, chi)
    produced = 0
    for _ts, raw, ch in cap.packets[:2000]:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        for out in fz.on_packet(mp, ch):
            out.SerializeToString()  # every emitted packet stays a valid MeshPacket
            produced += 1
    assert produced > 0
    assert len(fz.stats.counts) >= 4  # chaos exercised several fault kinds
    assert any(k in fz.stats.counts for k in ("corrupt_payload", "garbage_payload"))


def test_fuzz_campaigns_inject_expected_portnums():
    cap = sim.generate(nodes=20, days=1, seed=2, start=1_700_000_000)
    cfg = fuzz.preset("adversary", seed=8)
    # tighten intervals so every campaign fires within the test window
    cfg.evil_twin_interval = cfg.gps_spoofer_interval = 0.01
    cfg.forged_acks_interval = cfg.rogue_admin_interval = cfg.waypoint_spam_interval = 0.01
    fz = fuzz.Fuzzer(cfg, cap.nodes, {c: i for i, c in enumerate(cap.channels)})
    seen_portnums: set[int] = set()
    now = 1000.0
    for _ in range(200):
        now += 0.05
        for mp in fz.on_tick(now):
            mp.SerializeToString()
            seen_portnums.add(mp.decoded.portnum)
    # flooder->TEXT(1), gps->POSITION(3), evil_twin->NODEINFO(4),
    # forged_ack->ROUTING(5), rogue_admin->ADMIN(6), waypoint_spam->WAYPOINT(8)
    assert {1, 3, 4, 5, 6, 8}.issubset(seen_portnums)
    for kind in (
        "flooder",
        "gps_spoofer",
        "evil_twin",
        "forged_acks",
        "rogue_admin",
        "waypoint_spam",
    ):
        assert kind in fz.stats.counts, f"campaign {kind} never fired"


def test_fuzz_drop_and_duplicate_change_stream_volume():
    cap = sim.generate(nodes=15, days=1, seed=6, start=1_700_000_000)
    chi = {c: i for i, c in enumerate(cap.channels)}
    drop = fuzz.Fuzzer(fuzz.FuzzConfig(seed=1, drop=1.0), cap.nodes, chi)
    dup = fuzz.Fuzzer(fuzz.FuzzConfig(seed=1, duplicate=1.0), cap.nodes, chi)
    n_drop = n_dup = 0
    for _ts, raw, ch in cap.packets[:300]:
        a = mesh_pb2.MeshPacket()
        a.ParseFromString(raw)
        b = mesh_pb2.MeshPacket()
        b.ParseFromString(raw)
        n_drop += len(drop.on_packet(a, ch))
        n_dup += len(dup.on_packet(b, ch))
    assert n_drop == 0  # everything dropped
    assert n_dup == 600  # everything duplicated


# ── Channel-hash routing + PSK advertising (caller-supplied specs) ─────────
def test_channel_hash_matches_known_meshtastic_hashes():
    # generic Meshtastic facts: default-key preset channel hashes
    assert capture.channel_hash("LongFast", b"\x01") == 8
    assert capture.channel_hash("ShortTurbo", b"\x01") == 14


def test_from_sqlite_routes_by_ota_hash_with_caller_specs(tmp_path):
    import base64

    # caller owns the channel set: a keyed secondary (hash derived from name+psk),
    # a default public primary, and an explicit catch-all for unmatched hashes.
    key = bytes(range(32))
    secret_hash = capture.channel_hash("Secret", key)
    specs = [
        {"name": "LongFast", "psk": "AQ==", "primary": True},  # hash 8
        {"name": "Secret", "psk": base64.b64encode(key).decode()},  # derived hash
        {"name": "Unknown", "catch_all": True},
    ]
    cap = sim.generate(nodes=10, days=1, seed=1, start=1_700_000_000)
    chosen = [8, secret_hash, 4242]  # 4242 matches nothing -> catch-all
    rows = []
    for i, (ts, raw, _ch) in enumerate(cap.packets[:30]):
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        mp.channel = chosen[i % len(chosen)]
        rows.append((ts, mp.SerializeToString(), "x"))
    cap.packets = rows
    db = tmp_path / "c.db"
    _write_sqlite(db, cap)

    loaded = capture.from_sqlite(db, limit_nodes=0, channel_specs=specs)
    assert loaded.channels == ["LongFast", "Secret", "Unknown"]
    assert loaded.channel_specs is not None
    assert len(loaded.channel_specs[1].psk) == 32  # Secret's real key carried
    routed = {ch for _ts, _raw, ch in loaded.packets}
    assert routed == {"LongFast", "Secret", "Unknown"}  # incl. catch-all bucket


def test_resolve_channel_specs_passthrough():
    assert capture.resolve_channel_specs(None) is None
    assert capture.resolve_channel_specs([]) is None
    out = capture.resolve_channel_specs([{"name": "A", "psk": "AQ=="}])
    assert out and isinstance(out[0], capture.ChannelSpec) and out[0].name == "A"


# ── fit_profile (tune the sim from a real capture) ────────────────────────
def test_fit_profile_derives_mixes_and_intervals():
    cap = sim.generate(nodes=80, days=2, seed=4, start=1_700_000_000)
    prof = sim.fit_profile(cap)
    # mixes come from the node DB; weights are (name, count) pairs
    assert prof["hw_weights"] and all(isinstance(w, tuple) for w in prof["hw_weights"])
    assert prof["role_weights"]
    assert prof["channels"] == cap.channels
    assert prof["text_base_msgs_per_hour"] >= 0
    assert prof["telemetry_interval"] > 0
    assert set(prof["pos_interval"]) == {"mobile", "router", "default"}
    assert 1 in prof["portnum_mix"] or 67 in prof["portnum_mix"]
    # a profile fitted from a capture round-trips back through generate
    tuned = sim.generate(nodes=50, days=1, seed=2, profile=prof, channels=prof["channels"])
    assert tuned.channels == cap.channels
    assert len(tuned.packets) > 0


# ── Engine polish: observer position, Replay Clock, modem preset, connect hint ─
def test_capture_center_is_median_position():
    cap = sim.generate(nodes=40, days=1, seed=9, start=1_700_000_000)
    center = cap.center()
    assert center is not None
    lat, lon = center
    assert isinstance(lat, int) and isinstance(lon, int)
    # VLA-ish bounds for the synthetic venue
    assert 33_000_000_0 < lat < 35_000_000_0
    assert -109_000_000_0 < lon < -106_000_000_0


def test_replay_clock_and_observer_position_and_preset():
    cap = sim.generate(nodes=30, days=1, seed=3, start=1_700_000_000)
    sess = ReplaySession(
        "t",
        cap,
        ReplayParams(
            host="127.0.0.1",
            port=0,
            rate=800,
            node_delay=0,
            announce_interval=0.1,
            modem_preset="SHORT_TURBO",
            firmware_edition="DEFCON",
        ),
    )
    import socket as _sk

    probe = _sk.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess.params.port = port
    sess.start()
    try:
        deadline = time.time() + 5
        client = None
        while time.time() < deadline:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        assert client is not None
        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)
        observer_pos = None
        clock_seen = False
        preset = None
        announces = 0
        edition = None
        pio_env = None
        device_id_len = 0
        t0 = time.time()
        while time.time() - t0 < 3:
            fr = _read_frame(client)
            v = fr.WhichOneof("payload_variant")
            if v == "my_info":
                edition = fr.my_info.firmware_edition
                pio_env = fr.my_info.pio_env
                device_id_len = len(fr.my_info.device_id)
            if v == "node_info":
                if fr.node_info.num == 0x42524331:
                    observer_pos = (
                        fr.node_info.position.latitude_i,
                        fr.node_info.position.longitude_i,
                    )
                if fr.node_info.num == 0x5245504C:
                    clock_seen = True
            elif v == "config" and fr.config.HasField("lora"):
                preset = fr.config.lora.modem_preset
            elif v == "packet" and fr.packet.decoded.portnum == 1:
                announces += 1
        client.close()
        assert observer_pos == cap.center()  # "you are here" = capture center
        assert clock_seen  # Replay Clock node introduced
        assert preset == 8  # SHORT_TURBO
        assert announces >= 1  # kickoff + progress posted
        assert edition == mesh_pb2.FirmwareEdition.DEFCON  # event banner
        assert pio_env == "replay"
        assert device_id_len == 16
    finally:
        sess.stop()


def test_status_includes_connect_hint():
    from meshtastic_mcp.replay import get_manager

    cap = sim.generate(nodes=10, days=1, seed=1, start=1_700_000_000)
    mgr = get_manager()
    import socket as _sk

    probe = _sk.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    st = mgr.start(cap, ReplayParams(host="127.0.0.1", port=port, rate=100, node_delay=0))
    try:
        assert any(c.endswith(f":{port}") for c in st["connect"])
    finally:
        mgr.stop(st["id"])


# ── Builders + live injection + scenarios ──────────────────────────────
def test_build_waypoint_encodes_geofence_fields():
    from meshtastic_mcp.replay import build

    pl = build.waypoint_payload(
        37.0,
        -122.0,
        name="GF",
        geofence_radius=500,
        bbox=(36.9, -122.1, 37.1, -121.9),
        notify_on_enter=True,
        notify_on_exit=True,
    )
    # base fields parse with the (older) bundled proto; geofence fields appended raw
    w = mesh_pb2.Waypoint()
    w.ParseFromString(pl)
    assert w.name == "GF" and w.latitude_i == 370000000
    assert build._tag(9, 0) + build._varint(500) in pl  # geofence_radius
    assert build._tag(11, 0) + b"\x01" in pl  # notify_on_enter
    assert build._tag(10, 2) in pl  # bounding_box (length-delimited sub-message)


def test_from_kind_builds_each_packet_type():
    from meshtastic_mcp.replay import build

    cases = {
        "waypoint": {"lat": 1.0, "lon": 2.0, "geofence_radius": 100},
        "position": {"lat": 1.0, "lon": 2.0},
        "text": {"body": "hi"},
        "nodeinfo": {"id": "!00000001", "long_name": "N"},
        "raw": {"portnum": 70, "payload_hex": "deadbeef"},
    }
    want = {"waypoint": 8, "position": 3, "text": 1, "nodeinfo": 4, "raw": 70}
    for kind, args in cases.items():
        mp = build.from_kind(kind, args, from_node=0xABCD)
        assert mp.decoded.portnum == want[kind]
        assert getattr(mp, "from") == 0xABCD
    with pytest.raises(ValueError):
        build.from_kind("bogus", {}, from_node=1)


def test_from_events_builds_scenario_capture():
    cap = capture.from_events(
        [
            {"from": 0xA1, "kind": "nodeinfo", "args": {"id": "!000000a1", "long_name": "Trk"}},
            {
                "from": 0xA1,
                "kind": "waypoint",
                "args": {"lat": 37.0, "lon": -122.0, "geofence_radius": 500},
            },
            {"from": 0xA1, "kind": "position", "args": {"lat": 37.0, "lon": -122.0}, "delay": 5},
        ],
        start=1_700_000_000,
    )
    assert len(cap.packets) == 3
    assert len(cap.nodes) == 1 and cap.nodes[0].long_name == "Trk"
    ts = [p[0] for p in cap.packets]
    assert ts == sorted(ts) and ts[-1] - ts[0] == 6  # inter-event deltas 1 + 5


def test_live_inject_reaches_client():
    from meshtastic_mcp.replay import build, get_manager

    cap = sim.generate(nodes=20, days=1, seed=1, start=1_700_000_000)
    mgr = get_manager()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    st = mgr.start(cap, ReplayParams(host="127.0.0.1", port=port, rate=600, node_delay=0))
    sid = st["id"]
    try:
        client = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        assert client is not None
        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)
        time.sleep(0.3)
        mp = build.from_kind(
            "waypoint",
            {"lat": 40.0, "lon": -74.0, "geofence_radius": 250, "name": "INJ"},
            from_node=0xB2,
        )
        res = mgr.inject(sid, [mp], channel="LongFast")
        assert res["queued"] == 1
        found = False
        t0 = time.time()
        while time.time() - t0 < 3 and not found:
            fr = _read_frame(client)
            if fr.WhichOneof("payload_variant") == "packet" and fr.packet.decoded.portnum == 8:
                w = mesh_pb2.Waypoint()
                w.ParseFromString(fr.packet.decoded.payload)
                if w.name == "INJ":
                    found = True
        client.close()
        assert found  # injected waypoint reached the live client
        assert mgr.status(sid)["injected"] >= 1
    finally:
        mgr.stop(sid)


def test_port_in_use_raises_clear_error():
    from meshtastic_mcp.replay import PortInUseError, get_manager

    cap = sim.generate(nodes=5, days=1, seed=1, start=1_700_000_000)
    mgr = get_manager()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    a = mgr.start(cap, ReplayParams(host="127.0.0.1", port=port, rate=100, node_delay=0))
    try:
        with pytest.raises(PortInUseError):
            ReplaySession("dup", cap, ReplayParams(host="127.0.0.1", port=port)).start()
    finally:
        mgr.stop(a["id"])


def test_inject_through_fuzzer_mutates():
    # fuzz=True runs the injected packet through the active fuzz mutator
    from meshtastic_mcp.replay import build
    from meshtastic_mcp.replay import fuzz as fz

    cap = sim.generate(nodes=10, days=1, seed=1, start=1_700_000_000)
    chi = {c: i for i, c in enumerate(cap.channels)}
    fuzzer = fz.Fuzzer(fz.FuzzConfig(seed=1, garbage_payload=1.0), cap.nodes, chi)
    mp = build.from_kind("text", {"body": "clean message"}, from_node=1)
    before = mp.decoded.payload
    outs = fuzzer.on_packet(mp, "LongFast")
    assert outs[0].decoded.payload != before  # garbage_payload mutated it


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
