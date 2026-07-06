# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""WS0 realism harness: metrics schema, DEF CON log importer, golden profiles.

1. `metrics.capture_stats` produces the full stat schema, deterministically,
   over a sim capture.
2. `tools/import_defcon_logs.py` round-trips a synthetic gateway log (built in
   the DEF CON str(dict) format from our own protobufs) into the shared SQLite
   schema that `capture.from_sqlite` loads.
3. The committed golden profiles (aggregates of the real Burning Man 2025 /
   DEF CON 33 captures) parse, carry the schema, and contain no payloads.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from google.protobuf import text_format
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import capture, metrics, sim

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "src" / "meshtastic_mcp" / "replay" / "profiles"

# The stat profiles are generated locally from private datasets and are NOT
# committed (see .gitignore); the tests that assert against them skip when the
# files are absent (e.g. in CI).
requires_profiles = pytest.mark.skipif(
    not (PROFILES / "burningman2025.json").exists() or not (PROFILES / "defcon33.json").exists(),
    reason="locally generated golden profiles not present",
)

EXPECTED_KEYS = {
    "schema",
    "label",
    "packets",
    "span_hours",
    "pkts_per_hour",
    "nodes",
    "channels",
    "portnum_mix",
    "encrypted_fraction",
    "want_response_fraction",
    "hop_limit",
    "hop_start",
    "talker_skew",
    "text",
    "telemetry",
    "position",
    "timing",
    "rx",
    "dup_id_multiplicity",
    "tak_packets",
}


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_defcon_logs", REPO / "tools" / "import_defcon_logs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── capture_stats over a sim capture ─────────────────────────────────────────


def test_capture_stats_schema_and_determinism():
    cap = sim.generate(nodes=40, days=1, seed=9, start=1_700_000_000)
    stats_a = metrics.capture_stats(cap)
    stats_b = metrics.capture_stats(sim.generate(nodes=40, days=1, seed=9, start=1_700_000_000))
    assert stats_a == stats_b  # seeded generate -> identical stats
    assert set(stats_a) >= EXPECTED_KEYS
    assert stats_a["packets"] > 0
    assert stats_a["nodes"]["count"] == 40
    # portnum mix uses friendly names and covers the core apps
    for port in ("TELEMETRY", "POSITION", "NODEINFO", "TEXT_MESSAGE"):
        assert port in stats_a["portnum_mix"], stats_a["portnum_mix"]
    # encrypted traffic is measured
    assert 0 < stats_a["encrypted_fraction"] < 1
    # text stats populated with percentile summary
    assert stats_a["text"]["n"] > 0
    assert stats_a["text"]["len"]["p50"] is not None
    # telemetry variants seen
    assert stats_a["telemetry"]["variant_mix"].get("device_metrics", 0) > 0
    # everything must be JSON-serializable (profile files depend on it)
    json.dumps(stats_a)


def test_summarize_and_skew_helpers():
    s = metrics.summarize([float(x) for x in range(100)])
    assert s["n"] == 100 and s["min"] == 0 and s["max"] == 99 and s["p50"] == 50
    assert metrics.summarize([])["p50"] is None
    # NaN values are dropped, not propagated (real captures contain NaN humidity)
    assert metrics.summarize([1.0, float("nan"), 3.0])["n"] == 2
    from collections import Counter

    skew = metrics.talker_skew(Counter({1: 97, 2: 1, 3: 1, 4: 1}))
    assert skew["senders"] == 4
    assert skew["top10pct_share"] == 0.97


# ── DEF CON log importer round-trip (synthetic log, real format) ─────────────


def _dict_record(mp: mesh_pb2.MeshPacket) -> str:
    """Render a MeshPacket the way the DEF CON gateway logs do: a str(dict)
    with a top-level `'raw': <text-format>` block."""
    raw = text_format.MessageToString(mp)
    frm = getattr(mp, "from")
    return (
        f"{{'from': {frm}, 'to': {mp.to}, 'raw': {raw}, 'fromId': '!{frm:08x}', 'toId': '^all'}}\n"
    )


def _mk_packet(
    pid: int, frm: int, portnum: int, payload: bytes, rx_time: int
) -> mesh_pb2.MeshPacket:
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", frm)
    mp.to = 0xFFFFFFFF
    mp.id = pid
    mp.rx_time = rx_time
    mp.hop_limit = 2
    mp.hop_start = 3
    mp.rx_snr = 9.5
    mp.rx_rssi = -80
    mp.decoded.portnum = portnum
    mp.decoded.payload = payload
    return mp


def test_import_defcon_logs_roundtrip(tmp_path):
    imp = _load_importer()
    user = mesh_pb2.User(id="!0000002a", long_name="Synth Node", short_name="SYNT", hw_model=43)
    text = _mk_packet(101, 42, 1, b"hello synthetic mesh", 1_700_000_100)
    info = _mk_packet(102, 42, 4, user.SerializeToString(), 1_700_000_050)
    dup = _mk_packet(101, 42, 1, b"hello synthetic mesh", 1_700_000_130)
    dup.hop_limit = 1  # rebroadcast copy, one hop later
    enc = mesh_pb2.MeshPacket()
    setattr(enc, "from", 77)
    enc.to = 0xFFFFFFFF
    enc.id = 103
    enc.rx_time = 1_700_000_200
    enc.encrypted = b"\x01\x02\x03\x04"

    log = tmp_path / "synth_ShortTurbo.txt"
    log.write_text("".join(_dict_record(p) for p in (info, text, dup, enc)))
    db = tmp_path / "out.db"
    stats = imp.import_logs(str(db), [log])
    assert stats["records"] == 4 and stats["parsed"] == 4
    assert stats["packets"] == 3  # dup id collapses into packet, kept in packet_seen
    assert stats["seen"] == 4

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT long_name, short_name, hw_model FROM node WHERE node_id=42"
    ).fetchone()
    assert row == ("Synth Node", "SYNT", "HELTEC_V3")
    mult = conn.execute("SELECT COUNT(*) FROM packet_seen WHERE packet_id=101").fetchone()[0]
    assert mult == 2
    conn.close()

    cap = capture.from_sqlite(db, limit_nodes=0)
    assert cap.channels == ["ShortTurbo"]
    got = metrics.capture_stats(cap)
    assert got["packets"] == 3
    assert got["portnum_mix"]["TEXT_MESSAGE"] == 1
    assert got["encrypted_fraction"] == round(1 / 3, 3)
    extra = metrics.sqlite_extra_stats(str(db))
    assert extra["observation_multiplicity"] == {"1": 2, "2": 1}
    assert extra["rx"]["snr"]["n"] == 3  # encrypted record had no snr


# ── golden profiles ──────────────────────────────────────────────────────────


@requires_profiles
def test_golden_profiles_present_and_aggregate_only():
    for name in ("burningman2025", "defcon33"):
        data = json.loads((PROFILES / f"{name}.json").read_text())
        assert data["meta"]["name"] == name
        stats = data["stats"]
        assert stats["schema"] == metrics.SCHEMA_VERSION
        assert stats["packets"] > 100_000
        assert stats["nodes"]["count"] > 1_500
        # aggregates only: no channel names, no node names, no payload text
        assert "channels" not in stats
        assert "channel_count" in stats
        blob = json.dumps(stats).lower()
        for forbidden in ("long_name", "short_name", "payload", "psk", "public_key"):
            assert forbidden not in blob, forbidden
        # observation-level stats present (dup multiplicity + RX populations)
        assert stats["observed"]["observation_multiplicity"]["1"] > 0
        assert stats["observed"]["rx"]["rssi"]["p50"] is not None


@requires_profiles
def test_golden_profiles_capture_the_calibration_targets():
    """The measured deltas that drive WS1-WS3 stay visible in the fixtures."""
    dc = json.loads((PROFILES / "defcon33.json").read_text())["stats"]
    bm = json.loads((PROFILES / "burningman2025.json").read_text())["stats"]
    # DEF CON: ~44% encrypted, heavy rebroadcast duplication
    assert 0.35 < dc["encrypted_fraction"] < 0.55
    dup_multi = sum(v for k, v in dc["observed"]["observation_multiplicity"].items() if k != "1")
    assert dup_multi / dc["packets"] > 0.2
    # Burning Man: NODEINFO-dominated portnum mix, ROUTING is a first-class citizen
    assert bm["portnum_mix"]["NODEINFO"] > bm["portnum_mix"]["TELEMETRY"]
    assert bm["portnum_mix"]["ROUTING"] > 0.05 * bm["packets"]
    # both: text has a long tail our sim must reproduce
    assert dc["text"]["len"]["max"] > 200
    assert bm["text"]["len"]["max"] > 200


# ── WS3/WS-T: temporal coherence + sensor telemetry ──────────────────────────


def _telemetry(cap, variant):
    from meshtastic.protobuf import telemetry_pb2

    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") != "decoded" or mp.decoded.portnum != 67:
            continue
        tel = telemetry_pb2.Telemetry()
        tel.ParseFromString(mp.decoded.payload)
        if tel.WhichOneof("variant") == variant:
            yield tel


def test_battery_population_is_bimodal_with_zero_spike():
    from collections import Counter

    cap = sim.generate(nodes=400, days=2, seed=3, start=1_700_000_000)
    levels = Counter(
        min(t.device_metrics.battery_level, 101) for t in _telemetry(cap, "device_metrics")
    )
    assert levels[101] > 0  # plugged mode
    assert levels[0] > 0  # drained-to-zero spike (real captures show both)
    curve = sum(v for k, v in levels.items() if 0 < k < 101)
    assert curve > 0  # and a discharge curve in between


def test_chutil_tracks_generated_load():
    import math
    import statistics
    from collections import Counter, defaultdict

    start = 1_700_000_000
    cap = sim.generate(nodes=400, days=2, seed=3, start=start)
    by_hour_n = Counter((t - start) // 3600 for t, _r, _c in cap.packets)
    by_hour_util = defaultdict(list)
    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") == "decoded" and mp.decoded.portnum == 67:
            from meshtastic.protobuf import telemetry_pb2

            tel = telemetry_pb2.Telemetry()
            tel.ParseFromString(mp.decoded.payload)
            if tel.WhichOneof("variant") == "device_metrics":
                h = (tel.time - start) // 3600
                by_hour_util[h].append(tel.device_metrics.channel_utilization)
    hours = sorted(h for h in by_hour_util if len(by_hour_util[h]) >= 3)
    xs = [by_hour_n[h] for h in hours]
    ys = [statistics.mean(by_hour_util[h]) for h in hours]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    rho = cov / math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    assert rho > 0.5, f"chutil should track the diurnal load envelope (rho={rho:.2f})"


def test_env_telemetry_personas_and_nan():
    import math

    cap = sim.generate(nodes=400, days=2, seed=3, start=1_700_000_000)
    envs = list(_telemetry(cap, "environment_metrics"))
    assert envs
    # every persona reports temperature; only subsets report lux/humidity/pressure
    assert all(t.environment_metrics.HasField("temperature") for t in envs)
    n_lux = sum(t.environment_metrics.HasField("lux") for t in envs)
    n_hum = sum(t.environment_metrics.HasField("relative_humidity") for t in envs)
    assert 0 < n_lux < len(envs)
    assert 0 < n_hum < len(envs)
    # a boosted NaN fraction survives serialization (real sensors emit NaN)
    prof = {"climate": {"t_mean": 22.0, "t_amp": 9.0, "pressure_hpa": 780.0, "nan_fraction": 0.5}}
    cap2 = sim.generate(nodes=400, days=2, seed=3, start=1_700_000_000, profile=prof)
    hums = [
        t.environment_metrics.relative_humidity
        for t in _telemetry(cap2, "environment_metrics")
        if t.environment_metrics.HasField("relative_humidity")
    ]
    assert sum(math.isnan(h) for h in hums) > 0


def test_text_spike_multiplies_hourly_budget():
    from collections import Counter

    start = 1_700_000_000
    prof = {"spikes": [{"start_h": 10, "hours": 2, "text_x": 10.0}]}
    cap = sim.generate(nodes=300, days=1, seed=5, start=start, profile=prof)
    texts = Counter()
    for t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") == "decoded" and mp.decoded.portnum == 1:
            texts[(t - start) // 3600] += 1
    spike = (texts[10] + texts[11]) / 2
    rest = [v for h, v in texts.items() if h not in (10, 11)]
    assert spike > 2 * max(rest, default=0)


# ── WS4: scenario presets, profile plumbing, fit_profile v2 ──────────────────


def test_scenario_presets_shape_the_mesh():
    from meshtastic_mcp.replay import sim as _sim

    assert set(_sim.PRESETS) == {"meshcon", "burningman", "defcon"}
    bm = _sim.generate(nodes=500, days=2, seed=7, start=1_700_000_000, profile="burningman")
    dc = _sim.generate(nodes=500, days=2, seed=7, start=1_700_000_000, profile="defcon")
    assert bm.label.startswith("burningman") and dc.label.startswith("defcon")
    # each preset uses its own published channel lineup
    assert "Everyone" in bm.channels and "DEFCONnect" in dc.channels
    bs = metrics.capture_stats(bm)
    ds = metrics.capture_stats(dc)
    # DEF CON runs much more foreign/encrypted traffic than the playa
    assert ds["encrypted_fraction"] > 0.35
    assert bs["encrypted_fraction"] < 0.25
    # both presets enable the observer -> RX metadata + rebroadcast duplicates
    assert ds["rx"]["rssi"]["n"] > 0
    dup2 = sum(int(v) for k, v in ds["dup_id_multiplicity"].items() if k != "1")
    assert dup2 > 0
    # DEF CON cranks a hop-7 subpopulation harder than the default
    assert int(ds["hop_start"].get("7", 0)) > 0


def test_profile_accepts_dict_json_and_deep_merges(tmp_path):
    from meshtastic_mcp.replay import sim as _sim

    # nested config dicts deep-merge (observer.enabled stays off unless set)
    prof = {"observer": {"path_loss_exp": 3.5}}
    merged = _sim._resolve_profile(prof)
    assert merged["observer"]["path_loss_exp"] == 3.5
    assert merged["observer"]["enabled"] is False  # untouched default preserved
    assert merged["venue"]["name"] == _sim.PROFILE["venue"]["name"]
    # JSON path load
    import json

    p = tmp_path / "prof.json"
    p.write_text(json.dumps({"text_base_msgs_per_hour": 1.0, "encrypted_fraction": 0.0}))
    cap = _sim.generate(nodes=80, days=1, seed=1, start=1_700_000_000, profile=str(p))
    assert cap.packets
    assert metrics.capture_stats(cap)["encrypted_fraction"] == 0.0


def test_fit_profile_v2_emits_full_schema_and_round_trips():
    from meshtastic_mcp.replay import sim as _sim

    src = _sim.generate(nodes=300, days=2, seed=4, start=1_700_000_000, profile="defcon")
    prof = _sim.fit_profile(src)
    for key in (
        "hw_weights",
        "role_weights",
        "hop_start_weights",
        "encrypted_fraction",
        "text_dm_fraction",
        "text_channel_weights",
        "pos_interval",
        "telemetry_interval",
    ):
        assert key in prof, key
    # a capture generated from the fitted profile is comparable on encryption
    regen = _sim.generate(nodes=300, days=2, seed=9, start=1_700_000_000, profile=prof)
    a = metrics.capture_stats(src)["encrypted_fraction"]
    b = metrics.capture_stats(regen)["encrypted_fraction"]
    assert abs(a - b) < 0.12


def test_ninja_fuzz_preset_spoofs_nodeinfo_without_key_change():
    from meshtastic.protobuf import mesh_pb2

    from meshtastic_mcp.replay import fuzz
    from meshtastic_mcp.replay import sim as _sim

    assert "ninja" in fuzz.PRESET_NAMES
    cfg = fuzz.preset("ninja", seed=3)
    assert cfg.ninja_flood and cfg.ninja_flood_batch > 0
    cap = _sim.generate(nodes=40, days=1, seed=5, start=1_700_000_000)
    ch_index = {c: i for i, c in enumerate(cap.channels)}
    f = fuzz.Fuzzer(cfg, cap.nodes, ch_index)
    # drive the time-based campaign
    out = []
    for i in range(400):
        out += f.on_tick(1_000_000.0 + i)
    assert out, "ninja campaign should emit NodeInfo spoofs"
    real_nums = {n.num for n in cap.nodes}
    spoofed = 0
    for mp in out:
        assert mp.decoded.portnum == 4  # NODEINFO
        u = mesh_pb2.User()
        u.ParseFromString(mp.decoded.payload)
        assert getattr(mp, "from") in real_nums  # uses a real node's number
        assert not u.public_key  # no key change -> dodges the app's warning
        if "🥷" in u.long_name or "🥷" in u.short_name:
            spoofed += 1
    assert spoofed > 0
    assert f.status()["counts"].get("ninja_flood", 0) > 0


# ── WS-A: opt-in ATAK squad ──────────────────────────────────────────────────


def test_tak_squad_is_opt_in_and_well_formed():
    from meshtastic.protobuf import atak_pb2

    from meshtastic_mcp.replay import sim as _sim

    # off by default and in the fitted event presets
    for profile in (None, "defcon", "burningman"):
        cap = _sim.generate(nodes=150, days=1, seed=8, start=1_700_000_000, profile=profile)
        assert metrics.capture_stats(cap)["tak_packets"] == 0

    prof = {"tak": {"team_nodes": 5, "pli_interval": 45, "chat_per_hour": 3, "team": "Cyan"}}
    cap = _sim.generate(nodes=200, days=1, seed=8, start=1_700_000_000, profile=prof)
    stats = metrics.capture_stats(cap)
    assert stats["tak_packets"] > 0
    assert "ATAK_PLUGIN" in stats["portnum_mix"]

    pli = chat = 0
    callsigns = set()
    teams = set()
    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") != "decoded" or mp.decoded.portnum != 72:
            continue
        tp = atak_pb2.TAKPacket()
        tp.ParseFromString(mp.decoded.payload)  # must be a valid TAKPacket
        callsigns.add(tp.contact.callsign)
        teams.add(tp.group.team)
        assert tp.status.battery <= 100
        if tp.HasField("pli"):
            pli += 1
            assert -900_000_000 <= tp.pli.latitude_i <= 900_000_000
        if tp.HasField("chat"):
            chat += 1
            assert tp.chat.message
    assert pli > 0 and chat > 0
    assert len(callsigns) == 5  # one per squad member
    assert teams == {atak_pb2.Team.Value("Cyan")}


# ── capture-stats CLI subcommand ─────────────────────────────────────────────


def test_capture_stats_cli_preset_json(capsys):
    import pytest

    from meshtastic_mcp import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["capture-stats", "meshcon", "--sim-nodes", "60", "--sim-days", "1", "--json"])
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == metrics.SCHEMA_VERSION
    assert out["nodes"]["count"] == 60
    assert "portnum_mix" in out


def test_capture_stats_cli_text_render(capsys):
    import pytest

    from meshtastic_mcp import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["capture-stats", "defcon", "--sim-nodes", "60", "--sim-days", "1"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "capture: defcon" in text
    assert "portnum mix:" in text
    assert "NODEINFO" in text


def test_capture_stats_cli_bad_source_errors(capsys):
    import pytest

    from meshtastic_mcp import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["capture-stats", "/nonexistent/path.db"])
    assert exc.value.code == 1
    assert "error:" in capsys.readouterr().err
