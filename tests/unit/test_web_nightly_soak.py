# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Soak service: sink capture format, the channel-preflight guarantee, traffic
injection, and snapshot collection — all with faked hardware."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_cameras as rc
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly_soak
from meshtastic_mcp.web.services.nightly import NightlyConfig


class FakeSerialMon:
    def __init__(self) -> None:
        self.sinks: list = []
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, serial: str) -> None:
        self.acquired.append(serial)

    async def release(self, serial: str) -> None:
        self.released.append(serial)


class FakePortLocks:
    def __init__(self) -> None:
        self.guarded: list[str] = []
        self._held: set[str] = set()

    @asynccontextmanager
    async def guard(self, serial: str):
        # Enforce the real contract: a device's port is held exclusively — no
        # overlapping guard for the same serial.
        assert serial not in self._held, f"{serial} port guarded re-entrantly"
        self._held.add(serial)
        self.guarded.append(serial)
        try:
            yield
        finally:
            self._held.discard(serial)


class Observations:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    async def __call__(self, severity, kind, message, data) -> None:
        self.items.append((severity, kind, message))

    def kinds(self) -> list[str]:
        return [k for _s, k, _m in self.items]


async def _seed_device(db, serial="S1", port="/dev/ttyUSB0", env="heltec-v3"):
    await db.execute(
        "INSERT INTO devices (serial_number, current_port, env, online, kind) "
        "VALUES (?,?,?,1,'usb')",
        (serial, port, env),
    )


def _soak(db, tmp_path: Path, obs, cfg=None, nightly_id=7, keepalive=None):
    return nightly_soak.NightlySoak(
        db,
        FakeSerialMon(),
        FakePortLocks(),
        cfg=cfg or NightlyConfig(),
        nightly_id=nightly_id,
        data_dir=tmp_path / "night",
        observe=obs,
        keepalive=keepalive,
    )


def test_sink_writes_logs_and_telemetry(tmp_path: Path):
    async def go():
        db = await Database(tmp_path / "db").connect()
        obs = Observations()
        soak = _soak(db, tmp_path, obs)
        data = tmp_path / "night"
        data.mkdir(parents=True)
        logs = nightly_soak._JsonlWriter(data / nightly_soak.LOGS_FILE)
        telem = nightly_soak._JsonlWriter(data / nightly_soak.TELEMETRY_FILE)
        sink = soak._make_sink(logs, telem)

        sink({"ts": 1.0, "serial": "S1", "port": "/dev/x", "line": "INFO boot", "level": "INFO"})
        sink({"ts": 2.0, "serial": "S1", "port": "/dev/x", "line": "x", "heap_free": 92344})
        sink(
            {
                "ts": 3.0,
                "serial": "S2",
                "port": "/dev/y",
                "line": "Battery: usbPower=0, isCharging=0, batMv=4011, batPct=87",
            }
        )
        logs.close()
        telem.close()

        log_rows = [
            json.loads(ln) for ln in (data / nightly_soak.LOGS_FILE).read_text().splitlines()
        ]
        assert len(log_rows) == 3 and log_rows[0]["level"] == "INFO"
        telem_rows = [
            json.loads(ln) for ln in (data / nightly_soak.TELEMETRY_FILE).read_text().splitlines()
        ]
        assert {(t["kind"], t["value"]) for t in telem_rows} == {("heap", 92344), ("battery", 87)}
        assert soak.summary.lines == {"S1": 2, "S2": 1}
        await db.close()

    asyncio.run(go())


def test_preflight_flags_default_channel_and_unset_region(tmp_path: Path, monkeypatch):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        await _seed_device(db, "BAD", "/dev/b")
        await _seed_device(db, "NOREGION", "/dev/c")

        def fake_info(port, timeout_s=8.0):
            return {
                "/dev/a": {"primary_channel": "McpTest", "region": "US"},
                "/dev/b": {"primary_channel": "LongFast", "region": "US"},
                "/dev/c": {"primary_channel": "McpTest", "region": "UNSET"},
            }[port]

        monkeypatch.setattr(nightly_soak.mt_info, "device_info", fake_info)
        obs = Observations()
        soak = _soak(db, tmp_path, obs)
        await soak._preflight()

        kinds = obs.kinds()
        assert kinds.count("channel.default_profile") == 1
        assert kinds.count("channel.region_unset") == 1
        assert soak.summary.preflight_failures == 2
        bad = next(m for _s, k, m in obs.items if k == "channel.default_profile")
        assert "BAD" in bad and "LongFast" in bad
        await db.close()

    asyncio.run(go())


def test_run_sends_traffic_and_snapshots(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.05)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    sent: list[str] = []

    def fake_send(text, to, channel_index, want_ack, port):
        sent.append(text)
        return {"ok": True}

    def fake_snapshot(device_index, *, rotation=0, mirror=False):
        return b"\xff\xd8fakejpeg"

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1", "/dev/a")
        cid = await rc.add(db, name="cam0", device_index="0")
        await rc.assign(db, cid, "S1")

        monkeypatch.setattr(nightly_soak.admin, "send_text", fake_send)
        monkeypatch.setattr(
            nightly_soak.mt_info,
            "device_info",
            lambda port, timeout_s=8.0: {
                "primary_channel": "McpTest",
                "region": "US",
            },
        )
        monkeypatch.setattr(nightly_soak.camera_stream, "snapshot", fake_snapshot)

        obs = Observations()
        cfg = NightlyConfig(soak_traffic_interval_min=0.001, soak_snapshot_interval_min=0.001)
        soak = _soak(db, tmp_path, obs, cfg=cfg, nightly_id=9)
        summary = await soak.run(duration_s=0.4)

        assert summary.sends_attempted >= 1 and summary.sends_failed == 0
        assert sent and all(t.startswith("nightly-9-") for t in sent)
        sends_file = tmp_path / "night" / nightly_soak.SENDS_FILE
        rows = [json.loads(ln) for ln in sends_file.read_text().splitlines()]
        assert rows[0]["ok"] is True and rows[0]["text"] == "nightly-9-0"
        assert summary.snapshots and (tmp_path / "night" / summary.snapshots[0]).exists()
        # The soak must hold a monitor open on the fleet device (else the sink
        # sees nothing at 3am) and release it at the end.
        assert soak.serialmon.acquired == ["S1"]
        assert soak.serialmon.released == ["S1"]
        await db.close()

    asyncio.run(go())


def test_no_transmit_to_misbaked_device(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.02)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)
    sent: list[str] = []

    def fake_send(text, to, channel_index, want_ack, port):
        sent.append(text)
        return {"ok": True}

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        await _seed_device(db, "BAD", "/dev/b")

        # GOOD is on the private channel; BAD is on LongFast (misbaked).
        def fake_info(port, timeout_s=8.0):
            return {
                "/dev/a": {"primary_channel": "McpTest", "region": "US"},
                "/dev/b": {"primary_channel": "LongFast", "region": "US"},
            }[port]

        monkeypatch.setattr(nightly_soak.mt_info, "device_info", fake_info)
        monkeypatch.setattr(nightly_soak.admin, "send_text", fake_send)
        obs = Observations()
        cfg = NightlyConfig(soak_traffic_interval_min=0.001)
        soak = _soak(db, tmp_path, obs, cfg=cfg, nightly_id=3)
        await soak.run(duration_s=0.3)

        # Only GOOD is transmitted to; BAD (on LongFast) is never sent traffic.
        assert soak._verified == {"GOOD"}
        assert "channel.default_profile" in obs.kinds()
        sends = tmp_path / "night" / nightly_soak.SENDS_FILE
        rows = [json.loads(ln) for ln in sends.read_text().splitlines()] if sends.exists() else []
        assert rows and all(r["serial"] == "GOOD" for r in rows)
        await db.close()

    asyncio.run(go())


def test_send_seq_resumes_from_existing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.02)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)
    sent: list[str] = []

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        monkeypatch.setattr(
            nightly_soak.mt_info,
            "device_info",
            lambda port, timeout_s=8.0: {"primary_channel": "McpTest", "region": "US"},
        )
        monkeypatch.setattr(
            nightly_soak.admin,
            "send_text",
            lambda text, to, ch, ack, port: sent.append(text) or {"ok": True},
        )
        # Pre-seed 3 earlier sends (simulating a pre-restart soak).
        (tmp_path / "night").mkdir(parents=True)
        (tmp_path / "night" / nightly_soak.SENDS_FILE).write_text(
            '{"seq":0}\n{"seq":1}\n{"seq":2}\n'
        )
        soak = _soak(
            db,
            tmp_path,
            Observations(),
            cfg=NightlyConfig(soak_traffic_interval_min=0.001),
            nightly_id=5,
        )
        await soak.run(duration_s=0.2)
        # Sequence continues at 3 — no id already on the wire is reused.
        assert sent and sent[0] == "nightly-5-3"
        await db.close()

    asyncio.run(go())


def test_run_cancel_and_empty_fleet(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    async def go():
        db = await Database(tmp_path / "db").connect()
        obs = Observations()
        soak = _soak(db, tmp_path, obs)
        cancel = asyncio.Event()
        cancel.set()
        summary = await soak.run(duration_s=30.0, cancel=cancel)
        # Cancel short-circuits the loop; no-fleet + no-cameras are observed.
        assert summary.ended_at > 0
        assert "soak.no_fleet" in obs.kinds()
        assert "soak.no_cameras" in obs.kinds()
        await db.close()

    asyncio.run(go())
