# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Analyzer heuristics over synthetic soak JSONL + registry rows, and the
optional local-model behavioral pass (faked)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly_analysis as na
from meshtastic_mcp.web.services import nightly_soak
from meshtastic_mcp.web.services.nightly import NightlyConfig

NIGHTLY = {
    "id": 1,
    "started_at": 1000.0,
    "soak_started_at": 2000.0,
    "finished_at": 9000.0,
    "fw_sha_before": "a" * 40,
    "fw_sha_after": "b" * 40,
    "mcp_sha_before": "c" * 40,
    "mcp_sha_after": "c" * 40,
}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


async def _seed_device(db, serial, *, online=1, env="heltec-v3", last_seen=5000.0, name=None):
    await db.execute(
        "INSERT INTO devices (serial_number, friendly_name, env, online, last_seen, kind) "
        "VALUES (?,?,?,?,?,'usb')",
        (serial, name, env, online, last_seen),
    )


def _analyze(db, tmp_path, *, nightly=None, run=None, results=None, pipeline_obs=None):
    return na.analyze(
        db,
        cfg=NightlyConfig(),
        nightly=nightly or dict(NIGHTLY),
        run=run,
        results=results or [],
        pipeline_obs=pipeline_obs or [],
        data_dir=tmp_path / "night",
    )


def _cats(result: na.AnalysisResult) -> list[str]:
    return [o.category for o in result.observations]


def test_soak_panic_errors_reboots_and_silence(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: False)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "NOISY")
        await _seed_device(db, "QUIET")
        rows = [
            {"ts": 2100, "serial": "NOISY", "line": "Guru Meditation Error", "level": None},
            {"ts": 2200, "serial": "NOISY", "line": "ERROR | boom", "level": "ERROR"},
            {"ts": 2300, "serial": "NOISY", "line": "INFO | up", "level": "INFO", "uptime_s": 500},
            {"ts": 2400, "serial": "NOISY", "line": "INFO | up", "level": "INFO", "uptime_s": 12},
        ]
        _write_jsonl(tmp_path / "night" / nightly_soak.LOGS_FILE, rows)
        res = await _analyze(db, tmp_path)
        cats = _cats(res)
        assert "panic" in cats
        assert "error_logs" in cats
        assert "reboot_churn" in cats  # uptime 500 → 12 is a reset
        assert "log_silence" in cats  # QUIET produced nothing
        # llm gate off → explicitly reported, deterministic results stand.
        assert "llm_unavailable" in cats
        silence = next(o for o in res.observations if o.category == "log_silence")
        assert silence.device == "QUIET"
        await db.close()

    asyncio.run(go())


def test_telemetry_slopes_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: False)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1")
        _write_jsonl(
            tmp_path / "night" / nightly_soak.LOGS_FILE,
            [{"ts": 2100, "serial": "S1", "line": "x", "level": None}],
        )
        telem = []
        # Battery: 12 samples falling 1%/min → warn (threshold -0.2).
        for i in range(12):
            telem.append({"ts": 2000 + i * 60, "serial": "S1", "kind": "battery", "value": 90 - i})
        # Heap: 25 samples falling 50 B/min → NOT a leak (threshold -100).
        for i in range(25):
            telem.append(
                {"ts": 2000 + i * 60, "serial": "S1", "kind": "heap", "value": 90000 - i * 50}
            )
        _write_jsonl(tmp_path / "night" / nightly_soak.TELEMETRY_FILE, telem)
        res = await _analyze(db, tmp_path)
        cats = _cats(res)
        assert "battery_drain" in cats and "heap_leak" not in cats
        drain = next(o for o in res.observations if o.category == "battery_drain")
        assert drain.data is not None and drain.data["samples"] == 12
        await db.close()

    asyncio.run(go())


def test_traffic_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: False)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "A")
        await _seed_device(db, "B")
        _write_jsonl(
            tmp_path / "night" / nightly_soak.LOGS_FILE,
            [
                {"ts": 2100, "serial": "A", "line": "sent nightly-1-0", "level": None},
                {"ts": 2110, "serial": "B", "line": "RX text: nightly-1-0", "level": None},
                {"ts": 2200, "serial": "A", "line": "sent nightly-1-1", "level": None},
            ],
        )
        _write_jsonl(
            tmp_path / "night" / nightly_soak.SENDS_FILE,
            [
                {"ts": 2100, "seq": 0, "serial": "A", "text": "nightly-1-0", "ok": True},
                {"ts": 2200, "seq": 1, "serial": "A", "text": "nightly-1-1", "ok": True},
            ],
        )
        res = await _analyze(db, tmp_path)
        loss = next(o for o in res.observations if o.category == "traffic_loss")
        assert loss.data == {"lost": 1, "sent": 2}
        assert any("nightly-1-1" in e for e in loss.evidence)
        await db.close()

    asyncio.run(go())


def test_device_missing_step_errors_and_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: False)
    monkeypatch.setattr(na, "commit_subjects", lambda *a, **k: ["abc123 Fix eviction"])

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GONE", online=0, name="t-echo")
        pipeline_obs = [
            {
                "step": "firmware_update",
                "severity": "error",
                "kind": "git.fetch_failed",
                "message": "network down",
                "data": None,
            },
            {
                "step": "soak",
                "severity": "error",
                "kind": "channel.default_profile",
                "message": "BAD on LongFast",
                "data": {"serial": "BAD"},
            },
            {"step": "suite", "severity": "info", "kind": "step.finished", "message": "ok"},
        ]
        res = await _analyze(db, tmp_path, pipeline_obs=pipeline_obs)
        cats = _cats(res)
        assert "device_missing" in cats
        assert cats.count("step_error") == 1  # info obs not lifted
        assert "channel_default" in cats
        version = next(o for o in res.observations if o.category == "version_change")
        assert version.evidence == ["abc123 Fix eviction"]
        assert version.data is not None and "compare/" in version.data["compare_url"]
        await db.close()

    asyncio.run(go())


def test_pipeline_broken_night_no_run(tmp_path, monkeypatch):
    """No suite ever ran (run=None) — analysis still yields the step errors."""
    monkeypatch.setattr(na, "has_local_model", lambda: False)

    async def go():
        db = await Database(tmp_path / "db").connect()
        nightly = dict(NIGHTLY, soak_started_at=None, fw_sha_after=None)
        res = await _analyze(
            db,
            tmp_path,
            nightly=nightly,
            pipeline_obs=[
                {
                    "step": "firmware_update",
                    "severity": "error",
                    "kind": "git.clone_failed",
                    "message": "disk full",
                }
            ],
        )
        assert _cats(res).count("step_error") == 1
        assert res.counts["failed"] == 0 and res.failures == []
        await db.close()

    asyncio.run(go())


def test_behavioral_map_reduce_and_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: True)
    calls = {"complete": [], "vision": []}

    def fake_complete(prompt, *, system=None, lane="default", num_predict=0, timeout=0):
        calls["complete"].append((lane, prompt[:40]))
        if lane == "fast":
            return "- device chunk summary"
        return "- fleet: S1 rebooted twice\n- fleet: quiet otherwise"

    def fake_vision(path, question, **kw):
        calls["vision"].append(path)
        return {"match": True, "answer": "yes", "evidence": "screen shows garbled text"}

    monkeypatch.setattr(na.local_model, "complete", fake_complete)
    monkeypatch.setattr(na.local_model, "vision_assert", fake_vision)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1")
        _write_jsonl(
            tmp_path / "night" / nightly_soak.LOGS_FILE,
            [
                {"ts": 2100, "serial": "S1", "line": f"INFO line {i}", "level": "INFO"}
                for i in range(5)
            ],
        )
        (tmp_path / "night" / "snap-S1-123.jpg").write_bytes(b"\xff\xd8fake")
        res = await _analyze(db, tmp_path)
        behavior = [o for o in res.observations if o.category == "behavior"]
        assert len(behavior) == 2  # fleet summary + flagged snapshot
        fleet = next(o for o in behavior if "behavioral summary" in o.summary)
        assert any("rebooted twice" in e for e in fleet.evidence)
        snap = next(o for o in behavior if "screen check" in o.summary)
        assert snap.device == "S1" and any("garbled" in e for e in snap.evidence)
        lanes = [lane for lane, _p in calls["complete"]]
        assert "fast" in lanes and "default" in lanes  # map then reduce
        await db.close()

    asyncio.run(go())


def test_device_rows_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "has_local_model", lambda: False)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1", name="heltec bench")
        _write_jsonl(
            tmp_path / "night" / nightly_soak.LOGS_FILE,
            [{"ts": 2100, "serial": "S1", "line": "INFO ok", "level": "INFO"}],
        )
        res = await _analyze(db, tmp_path)
        assert res.device_rows == [
            {
                "device": "heltec bench",
                "serial": "S1",
                "env": "heltec-v3",
                "online": True,
                "bake": "—",
                "soak_lines": 1,
                "panics": 0,
                "errors": 0,
            }
        ]
        await db.close()

    asyncio.run(go())
