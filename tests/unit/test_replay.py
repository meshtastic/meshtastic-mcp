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

ALL_PORTNUMS = {1, 3, 4, 5, 6, 8, 34, 37, 65, 66, 67, 70, 71}


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


def test_rate_is_accurately_paced():
    """A requested steady `rate` must actually be delivered.

    Regression for drift-prone pacing: the old loop slept a fixed `1/rate` each
    packet, so the per-packet protobuf work + OS wait-overshoot were *added* on
    top of every interval and the achieved rate sagged ~15-25% below target
    (requesting 120/s yielded ~99/s). Deadline-anchored pacing absorbs that, so
    the measured rate tracks the request closely. Tolerance is generous to stay
    non-flaky on a loaded CI box; the pre-fix behavior missed the lower bound.
    """
    cap = sim.generate(nodes=60, days=1, seed=13, start=1_700_000_000)
    target = 200.0
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess = ReplaySession(
        "paced",
        cap,
        ReplayParams(host="127.0.0.1", port=port, rate=target, node_delay=0, loop=True),
    )
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
        client.settimeout(10)
        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)
        # drain handshake, then skip the node-DB burst so we measure the stream
        t0 = time.time()
        while time.time() - t0 < 5 and not sess.state.connected:
            _read_frame(client)
        skip_until = time.time() + 0.4
        while time.time() < skip_until:
            _read_frame(client)
        # measure achieved stream rate over a ~2s window
        pkts = 0
        start = time.time()
        while time.time() - start < 2.0:
            fr = _read_frame(client)
            if fr.WhichOneof("payload_variant") == "packet":
                pkts += 1
        measured = pkts / (time.time() - start)
        client.close()
        assert 0.88 * target <= measured <= 1.15 * target, (
            f"measured {measured:.1f}/s vs {target}/s"
        )
    finally:
        sess.stop()


def test_client_disconnect_mid_stream_survives_with_loop():
    """A mid-stream client disconnect must not kill a `loop=True` session.

    Regression: the stream thread treated any send failure as session-fatal
    (`stop.set()`), so an app closing/reconnecting mid-pass tore down the whole
    listener despite `loop=True` (found live: a real app disconnect ended the
    session with 'Connection reset by peer'). Now the stream severs only its own
    connection; the accept loop keeps listening and a reconnect streams again.
    """

    def _connect(port):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=1)
            except OSError:
                time.sleep(0.05)
        return None

    def _handshake_and_get_packet(client):
        client.settimeout(10)
        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)
        t0 = time.time()
        while time.time() - t0 < 5:
            fr = _read_frame(client)
            if fr.WhichOneof("payload_variant") == "packet":
                return True
        return False

    cap = sim.generate(nodes=20, days=1, seed=8, start=1_700_000_000)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess = ReplaySession(
        "reconnect",
        cap,
        ReplayParams(host="127.0.0.1", port=port, rate=300, node_delay=0, loop=True),
    )
    sess.start()
    try:
        first = _connect(port)
        assert first is not None
        assert _handshake_and_get_packet(first)  # stream is flowing
        # hard-disconnect mid-stream (app closed / reset by peer)
        first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        first.close()
        # the session must survive: not ended, stop not set
        deadline = time.time() + 3
        while time.time() < deadline and sess.state.connected:
            time.sleep(0.05)
        assert not sess.state.stop.is_set()
        assert sess.state.ended is False
        # and a reconnect must handshake + stream again
        second = _connect(port)
        assert second is not None, "listener gone after mid-stream disconnect"
        assert _handshake_and_get_packet(second), "no stream after reconnect"
        second.close()
    finally:
        sess.stop()


def test_duration_derives_steady_rate():
    """`duration` compresses the whole windowed capture into a fixed wall-clock
    span by deriving a steady rate of `len(window) / duration`, overriding any
    `rate`. Status must surface the duration mode + target/achieved rate.
    """
    cap = sim.generate(nodes=40, days=1, seed=17, start=1_700_000_000)
    n = len(cap.packets)
    assert n > 0
    sess = ReplaySession(
        "dur",
        cap,
        # duration wins over the (contradictory) rate below
        ReplayParams(host="127.0.0.1", port=0, rate=999.0, duration=30.0, node_delay=0),
    )
    assert sess.params.rate == pytest.approx(n / 30.0)
    from meshtastic_mcp.replay.engine import _status_dict

    st = _status_dict(sess)
    assert st["target_rate"] == pytest.approx(round(n / 30.0, 2))
    assert "duration 30.0s" in st["mode"]


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


def test_fromradio_from_kind_builds_fileinfo():
    from meshtastic_mcp.replay import build

    fr = build.fromradio_from_kind("fileinfo", {"file_name": "log.bin", "size_bytes": 4096})
    assert fr.WhichOneof("payload_variant") == "fileInfo"
    assert fr.fileInfo.file_name == "log.bin"
    assert fr.fileInfo.size_bytes == 4096
    # Adversarial negative size_bytes must still encode (masked into uint32) rather
    # than raising -- replay_inject_fileinfo advertises negative values as a fuzz case.
    neg = build.fromradio_from_kind("fileinfo", {"file_name": "x", "size_bytes": -1})
    assert neg.fileInfo.size_bytes == 0xFFFFFFFF
    with pytest.raises(ValueError):
        build.fromradio_from_kind("bogus", {})


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


def test_live_inject_fromradio_reaches_client():
    """inject_fromradio() is the handshake-only counterpart to inject(): a raw FromRadio
    (e.g. fileInfo) with no MeshPacket envelope, delivered outside the initial handshake
    window. Regression coverage for the gap found while adversarial-fuzz-testing
    Meshtastic-Android: there was no way to exercise a client's FileInfo handler
    (STATE_SEND_FILEMANIFEST) mid-session, only at connect time.
    """
    from meshtastic_mcp.replay import build, get_manager

    cap = sim.generate(nodes=5, days=1, seed=1, start=1_700_000_000)
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

        fr_in = build.fromradio_from_kind(
            "fileinfo", {"file_name": "evil.bin", "size_bytes": 999999999}
        )
        res = mgr.inject_fromradio(sid, [fr_in])
        assert res["queued"] == 1
        found = False
        t0 = time.time()
        while time.time() - t0 < 3 and not found:
            fr = _read_frame(client)
            if (
                fr.WhichOneof("payload_variant") == "fileInfo"
                and fr.fileInfo.file_name == "evil.bin"
            ):
                found = True
        client.close()
        assert found  # injected FileInfo reached the live client
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


def test_idle_reader_does_not_trip_send_timeout():
    """Regression for the `client.settimeout()` bug (found 2026-07-01 against a real
    Android app): a shared socket timeout bounded *both* send() and recv(), so a
    client that legitimately went quiet for `send_timeout` seconds (normal for this
    push-mostly protocol — the client only sends ToRadio for user actions/heartbeats)
    got its connection killed and misreported as "timed out", as if the client itself
    had stalled. Fixed via real SO_SNDTIMEO (`_set_send_timeout`), which only bounds
    sends. This test connects, completes the handshake, then goes idle on the client
    side for longer than `send_timeout` — the session must still be connected
    afterward, with no error, and still able to deliver stream packets.
    """
    cap = sim.generate(nodes=10, days=1, seed=5, start=1_700_000_000)
    params = ReplayParams(host="127.0.0.1", port=0, rate=50, node_delay=0, send_timeout=0.3)
    sess = ReplaySession("idle-reader", cap, params)
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
        assert client is not None

        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)
        # drain the handshake burst so nothing lingers in the socket buffer
        t0 = time.time()
        while time.time() - t0 < 2:
            _read_frame(client)
            if sess.state.connected:
                break

        # Now go idle on the client side for > send_timeout (0.3s) — the old code
        # would have the server's recv() time out here and tear the connection down.
        time.sleep(1.0)

        assert sess.state.connected is True
        assert sess.state.error is None

        # and the connection must still be usable afterward
        fr = _read_frame(client)
        assert fr.WhichOneof("payload_variant") is not None
        client.close()
    finally:
        sess.stop()


def test_stop_unblocks_idle_client_thread():
    """`stop()` must shutdown() the connected client socket, not just close the
    listening socket — otherwise, since recv() is now intentionally left blocking
    with no timeout (see above), an idly-connected client's serve thread would
    never notice `state.stop` and `ended` would never become True.
    """
    cap = sim.generate(nodes=5, days=1, seed=2, start=1_700_000_000)
    params = ReplayParams(host="127.0.0.1", port=0, rate=50, node_delay=0, loop=False)
    sess = ReplaySession("stop-unblock", cap, params)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess.params.port = port
    sess.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        _send_toradio(client, want_config_id=69420)
        _read_frame(client)  # at least one reply -> handshake under way
        sess.stop()
        deadline = time.time() + 3
        while time.time() < deadline and not sess.state.ended:
            time.sleep(0.05)
        assert sess.state.ended is True
    finally:
        client.close()


def test_replay_session_writes_lifecycle_log(tmp_path, monkeypatch):
    """Every session gets its own append-only JSONL for post-run analysis —
    lifecycle events (create/listen/connect/disconnect/end), not just whatever a
    caller happened to poll live via `replay_status()`.
    """
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    cap = sim.generate(nodes=5, days=1, seed=1, start=1_700_000_000)
    params = ReplayParams(host="127.0.0.1", port=0, rate=50, node_delay=0, loop=False)
    sess = ReplaySession("log-test", cap, params)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess.params.port = port
    sess.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        _send_toradio(client, want_config_id=69420)
        _read_frame(client)
    finally:
        client.close()
        sess.stop()
        deadline = time.time() + 3
        while time.time() < deadline and not sess.state.ended:
            time.sleep(0.05)

    import json

    # _replay_log_dir() = _recorder_default_dir().parent / "replay-logs", and
    # _recorder_default_dir() = $MESHTASTIC_MCP_DATA_DIR/.mtlog, so both land
    # as siblings directly under tmp_path.
    log_path = tmp_path / "replay-logs" / "log-test.jsonl"
    assert log_path.is_file(), f"expected lifecycle log at {log_path}"
    kinds = [json.loads(line)["kind"] for line in log_path.read_text().splitlines()]
    assert "session_created" in kinds
    assert "listening" in kinds
    assert "client_connected" in kinds
    assert kinds[-1] in ("session_ended",)  # always the last event written


# ── Beacon (MESH_BEACON_APP = 37) ────────────────────────────────────────────


def test_build_beacon_payload_encodes_message_and_offers():
    from meshtastic_mcp.replay import build

    # message only
    pl = build.beacon_payload("hello mesh")
    assert b"hello mesh" in pl
    # field 1 (message) uses tag 0x0A (field=1, wire=2)
    assert pl.startswith(bytes([0x0A]))

    # with channel offer
    pl2 = build.beacon_payload(
        "join us",
        offer_channel_name="MeshCon",
        offer_channel_psk=b"\x01",
        offer_region="US",
        offer_preset="LONG_FAST",
    )
    assert b"join us" in pl2
    assert b"MeshCon" in pl2
    # field 3 (offer_region=US=1) → tag 0x18 + varint 1
    assert bytes([0x18, 0x01]) in pl2
    # field 4 (offer_preset=LONG_FAST=0) → tag 0x20 + varint 0
    assert bytes([0x20, 0x00]) in pl2


def test_from_kind_builds_beacon():
    from meshtastic_mcp.replay import build

    mp = build.from_kind(
        "beacon",
        {"message": "test beacon", "offer_region": "US", "offer_preset": "LONG_FAST"},
        from_node=0xBEAC,
    )
    assert mp.decoded.portnum == build.MESH_BEACON_APP  # 37
    assert b"test beacon" in mp.decoded.payload
    assert getattr(mp, "from") == 0xBEAC


def test_sim_emits_beacon_packets():
    """sim.generate must include MESH_BEACON_APP (portnum 37) with routers."""
    cap = sim.generate(nodes=60, days=2, seed=7, start=1_700_000_000)
    counts = _portnum_counts(cap)
    assert 37 in counts, "MESH_BEACON_APP (portnum 37) missing from synthetic capture"
    assert counts[37] >= 1


def test_sim_waypoints_include_geofenced_variants():
    """sim.generate must include at least some waypoints with geofence fields."""
    from meshtastic_mcp.replay import build

    cap = sim.generate(nodes=60, days=3, seed=42, start=1_700_000_000)
    geofenced = 0
    for _ts, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.decoded.portnum != 8:
            continue
        payload = mp.decoded.payload
        # The sim appends geofence fields 9/11/12 (geofence_radius,
        # notify_on_enter, notify_on_exit) as raw wire bytes. A bare tag-9
        # byte (0x48) can occur incidentally inside lat/lon varints, so require
        # both the geofence_radius tag *and* the notify enter/exit tag pair
        # (0x58 0x01 0x60 0x01) — an appended suffix that is highly unlikely to
        # collide with the standard Waypoint encoding.
        notify_pair = build._tag(11, 0) + build._varint(1) + build._tag(12, 0) + build._varint(1)
        if build._tag(9, 0) in payload and notify_pair in payload:
            geofenced += 1
    assert geofenced >= 1, "no geofenced waypoints found in synthetic capture"


def test_from_kind_waypoint_wires_description():
    """from_kind must forward `description` into the Waypoint payload (regression:
    the field was silently dropped, so injected waypoints had empty descriptions).
    """
    from meshtastic_mcp.replay import build

    mp = build.from_kind(
        "waypoint",
        {"lat": 37.7, "lon": -122.4, "name": "Perimeter", "description": "geofenced POI"},
        from_node=0xBEEF,
    )
    assert mp.decoded.portnum == 8
    w = mesh_pb2.Waypoint()
    w.ParseFromString(mp.decoded.payload)
    assert w.name == "Perimeter"
    assert w.description == "geofenced POI"


def test_sim_traceroutes_are_request_response_pairs():
    """The sim must emit traceroute *responses*, not just in-flight requests.

    Apps only surface traceroute responses — their logs gate on a nonzero
    `decoded.request_id` (a zero id is an in-flight request they ignore). A
    capture of request-style-only traceroutes therefore renders an empty
    traceroute log (found live streaming to the Apple app). Responses must
    reference a real request id and follow firmware RouteDiscovery semantics:
    intermediate relays only in route (endpoints implied by from/to), SNR lists
    one-per-receiving-hop (len == len(route) + 1), mirrored route_back.
    """
    cap = sim.generate(nodes=60, days=2, seed=7, start=1_700_000_000)
    requests: dict[int, mesh_pb2.MeshPacket] = {}
    responses = []
    for _ts, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.decoded.portnum != 70:
            continue
        if mp.decoded.request_id:
            responses.append(mp)
        else:
            requests[mp.id] = mp
    assert requests, "no traceroute requests in the sim"
    assert responses, "no traceroute responses in the sim (apps would log nothing)"
    for resp in responses:
        req = requests.get(resp.decoded.request_id)
        assert req is not None, "response references a request id that was never emitted"
        # response flows dest -> requester
        assert getattr(resp, "from") == req.to
        assert resp.to == getattr(req, "from")
        assert resp.hop_start > 0  # apps gate route-back rendering on this
        rd = mesh_pb2.RouteDiscovery()
        rd.ParseFromString(resp.decoded.payload)
        assert getattr(req, "from") not in rd.route  # endpoints implied, not listed
        assert req.to not in rd.route
        assert len(rd.snr_towards) == len(rd.route) + 1
        assert len(rd.snr_back) == len(rd.route_back) + 1
        assert list(rd.route_back) == list(reversed(rd.route))
    # requests are want_response probes with an empty RouteDiscovery
    for req in requests.values():
        assert req.decoded.want_response is True


# ── BBS/bot plane (PROFILE["bots"], opt-in) ─────────────────────────────────
def test_sim_bots_off_by_default():
    """Presets stay calibrated: no bot nodes, no tapbacks unless opted in."""
    cap = sim.generate(nodes=40, days=1, seed=9, start=1_700_000_000)
    bot_names = {ln for ln, _sn, _hw in sim._BOT_IDENTITIES}
    assert not any(n.long_name in bot_names for n in cap.nodes)
    mp = mesh_pb2.MeshPacket()
    for _ts, raw, _ch in cap.packets:
        mp.Clear()
        mp.ParseFromString(raw)
        assert not (mp.decoded.portnum == 1 and mp.decoded.emoji)


def test_sim_bots_scene():
    """`profile={"bots": {...}}` emits the full BBS scene: bot nodes in the DB,
    trigger/pile-on text storms, tapbacks threading real packet ids (one
    legendary message collecting `tapback_storm` reactions, its body carrying
    the word "tapback" so it's findable in an app search), per-bot beacons, and
    attendee→bot traceroute response pairs — deterministic per seed.
    """
    prof = {"bots": {"count": 5, "storms_per_day": 40, "tapback_storm": 30}}
    cap = sim.generate(nodes=60, days=1, seed=9, start=1_700_000_000, profile=prof)
    base = sim.generate(nodes=60, days=1, seed=9, start=1_700_000_000)
    assert len(cap.nodes) == len(base.nodes) + 5
    bot_nums = {
        n.num for n in cap.nodes if n.long_name in {ln for ln, _sn, _hw in sim._BOT_IDENTITIES}
    }
    assert len(bot_nums) == 5

    mp = mesh_pb2.MeshPacket()
    all_ids: set[int] = set()
    reactions: Counter = Counter()
    legendary_id = None
    bot_texts = bot_beacons = bot_tr_responses = 0
    for _ts, raw, _ch in cap.packets:
        mp.Clear()
        mp.ParseFromString(raw)
        all_ids.add(mp.id)
        frm = getattr(mp, "from")
        if mp.decoded.portnum == 1:
            if mp.decoded.emoji:
                assert mp.decoded.reply_id != 0  # a tapback always threads a message
                reactions[mp.decoded.reply_id] += 1
            else:
                if "tapback" in mp.decoded.payload.decode("utf-8", "replace"):
                    legendary_id = mp.id
                if frm in bot_nums:
                    bot_texts += 1
        elif mp.decoded.portnum == 37 and frm in bot_nums:
            bot_beacons += 1
        elif mp.decoded.portnum == 70 and mp.decoded.request_id and frm in bot_nums:
            bot_tr_responses += 1

    assert bot_texts > 0  # bots piled on triggers
    assert bot_beacons > 0  # bots advertise themselves
    assert bot_tr_responses > 0  # attendees traced the bots, bots answered
    assert sum(reactions.values()) > 0
    assert all(rid in all_ids for rid in reactions)  # every tapback threads a real packet
    assert legendary_id is not None, 'no searchable "tapback" message emitted'
    assert reactions[legendary_id] == 30  # the legendary storm hits tapback_storm exactly
    ts = [p[0] for p in cap.packets]
    assert ts == sorted(ts)  # merged scene stays time-ordered
    cap2 = sim.generate(nodes=60, days=1, seed=9, start=1_700_000_000, profile=prof)
    assert [p[0] for p in cap2.packets] == ts  # deterministic per seed


def test_from_kind_text_tapback():
    from meshtastic_mcp.replay import build

    mp = build.from_kind("text", {"body": "👍", "reply_id": 0xBEEF, "emoji": True}, from_node=1)
    assert mp.decoded.portnum == 1
    assert mp.decoded.reply_id == 0xBEEF
    assert mp.decoded.emoji == 1
    plain = build.from_kind("text", {"body": "hi"}, from_node=1)
    assert plain.decoded.reply_id == 0 and plain.decoded.emoji == 0


def test_from_kind_traceroute_request_id_builds_response():
    from meshtastic_mcp.replay import build

    mp = build.from_kind(
        "traceroute",
        {"route": [0x2222], "snr_towards": [10, 20], "request_id": 0xAB12},
        from_node=0x3333,
        to_node=0x1111,
    )
    assert mp.decoded.request_id == 0xAB12
    # default stays a request (0 = unset on the wire)
    mp2 = build.from_kind("traceroute", {"route": []}, from_node=1, to_node=2)
    assert mp2.decoded.request_id == 0


# ── Traceroute responder ──────────────────────────────────────────────────────


def test_build_traceroute_payload_encodes_route_and_snr():
    from meshtastic_mcp.replay import build

    route = [0xAAAA, 0xBBBB, 0xCCCC]
    snr_t = [40, -80, 10]
    snr_b = [30, -90, 5]
    pl = build.traceroute_payload(route, snr_towards=snr_t, snr_back=snr_b)
    rd = mesh_pb2.RouteDiscovery()
    rd.ParseFromString(pl)
    assert list(rd.route) == route
    assert list(rd.snr_towards) == snr_t
    assert list(rd.snr_back) == snr_b


def test_from_kind_builds_traceroute():
    from meshtastic_mcp.replay import build

    mp = build.from_kind(
        "traceroute",
        {"route": [0x1111, 0x2222], "snr_towards": [20, -40]},
        from_node=0x1111,
        to_node=0x2222,
    )
    assert mp.decoded.portnum == 70
    rd = mesh_pb2.RouteDiscovery()
    rd.ParseFromString(mp.decoded.payload)
    assert list(rd.route) == [0x1111, 0x2222]
    assert list(rd.snr_towards) == [20, -40]


def test_traceroute_responder_replies_to_client_request():
    """When the connected client sends a TRACEROUTE_APP request, the replay
    engine must synthesise a RouteDiscovery response addressed back to the
    requester.
    """
    from meshtastic.protobuf import portnums_pb2

    cap = sim.generate(nodes=20, days=1, seed=3, start=1_700_000_000)
    dest_num = cap.nodes[0].num

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sess = ReplaySession(
        "tr", cap, ReplayParams(host="127.0.0.1", port=port, rate=200, node_delay=0)
    )
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
        # create_connection leaves a 1s recv timeout, but the loops below run to
        # 3s deadlines and _read_frame() does a blocking recv(1); raise the socket
        # timeout above the longest wait so a brief stream pause can't trip a
        # premature socket.timeout before the loop's own deadline governs.
        client.settimeout(6)

        _send_toradio(client, want_config_id=69420)
        _send_toradio(client, want_config_id=69421)

        # drain the handshake (my_info + node_info + config phases)
        t0 = time.time()
        while time.time() - t0 < 3 and not sess.state.connected:
            _read_frame(client)

        # send a TRACEROUTE_APP request from the observer to dest_num
        req_mp = mesh_pb2.MeshPacket()
        req_mp.to = dest_num & 0xFFFFFFFF
        setattr(req_mp, "from", 0x42524331)  # OBSERVER_NUM
        req_mp.id = 0x1234ABCD
        req_mp.want_ack = True
        req_mp.decoded.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
        rd_req = mesh_pb2.RouteDiscovery()
        req_mp.decoded.payload = rd_req.SerializeToString()
        _send_toradio(client, packet=req_mp)

        # look for the RouteDiscovery response within 3 seconds
        response = None
        t0 = time.time()
        while time.time() - t0 < 3 and response is None:
            fr = _read_frame(client)
            if fr.WhichOneof("payload_variant") != "packet":
                continue
            if fr.packet.decoded.portnum != portnums_pb2.PortNum.TRACEROUTE_APP:
                continue
            response = fr.packet
        client.close()

        assert response is not None, (
            "no RouteDiscovery response from the replay traceroute responder"
        )
        assert response.decoded.request_id == 0x1234ABCD
        assert getattr(response, "from") == dest_num & 0xFFFFFFFF  # responder = traced node
        rd_resp = mesh_pb2.RouteDiscovery()
        rd_resp.ParseFromString(response.decoded.payload)
        # Firmware RouteDiscovery semantics: route lists *intermediate* hops only
        # — the requester and destination are implied by the packet from/to and
        # must not appear (apps add the endpoints when rendering). SNR lists have
        # one entry per receiving hop including the endpoint: len(route) + 1.
        assert 0x42524331 not in rd_resp.route  # OBSERVER_NUM (requester)
        assert dest_num & 0xFFFFFFFF not in rd_resp.route
        assert len(rd_resp.snr_towards) == len(rd_resp.route) + 1
        assert len(rd_resp.snr_back) == len(rd_resp.route_back) + 1
        assert list(rd_resp.route_back) == list(reversed(rd_resp.route))
        # apps gate the "route back" rendering on a nonzero hop_start
        assert response.hop_start > 0
    finally:
        sess.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
