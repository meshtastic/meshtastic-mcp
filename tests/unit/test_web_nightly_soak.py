# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Soak service: sink capture format, the channel-preflight guarantee, traffic
injection via the held API observers, port claim/suspend discipline, and
snapshot collection — all with faked hardware."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_cameras as rc
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly_soak
from meshtastic_mcp.web.services.nightly import NightlyConfig
from meshtastic_mcp.web.services.soak_observer import ObserverSummary


class FakeSerialMon:
    def __init__(self) -> None:
        self.suspended: list[str] = []
        self.resumed: list[str] = []

    async def suspend(self, serial: str) -> None:
        self.suspended.append(serial)

    async def resume(self, serial: str) -> None:
        self.resumed.append(serial)


class FakePortLocks:
    def __init__(self) -> None:
        self.claims: dict[str, str] = {}
        self.claim_log: list[tuple[str, str]] = []
        self.release_log: list[tuple[str, str]] = []

    def claim(self, serial: str, owner: str) -> None:
        current = self.claims.get(serial)
        assert current is None or current == owner, f"{serial} double-claimed"
        self.claims[serial] = owner
        self.claim_log.append((serial, owner))

    def release_claim(self, serial: str, owner: str) -> None:
        if self.claims.get(serial) == owner:
            self.claims.pop(serial)
        self.release_log.append((serial, owner))

    def claimed_by(self, serial: str) -> str | None:
        return self.claims.get(serial)


class FakeObserver:
    """Stands in for SoakObserver: devices open unless listed in fail_open;
    channel/region come from the per-serial config map."""

    instances: list[FakeObserver] = []
    config: dict[str, tuple[str | None, str | None]] = {}
    fail_open: set[str] = set()

    def __init__(self, *, on_record, on_telemetry, hub=None, node_map=None) -> None:
        self.on_record = on_record
        self.on_telemetry = on_telemetry
        self.node_map = node_map or {}
        self.summary = ObserverSummary()
        self.sent: list[tuple[str, str]] = []
        self.input_events: list[tuple[str, object]] = []
        self.health_ticks = 0
        self.closed = False
        FakeObserver.instances.append(self)

    async def open_device(self, serial: str, port: str) -> bool:
        if serial in FakeObserver.fail_open:
            self.summary.open_failed[serial] = "no such port"
            return False
        self.summary.opened.append(serial)
        return True

    def is_held(self, serial: str) -> bool:
        return serial in self.summary.opened

    def channel_and_region(self, serial: str):
        return FakeObserver.config.get(serial, (None, None))

    async def send_text(self, serial: str, text: str, channel_index: int = 0):
        if serial not in self.summary.opened:
            raise RuntimeError(f"{serial}: no live soak observer session")
        self.sent.append((serial, text))
        return 1234

    async def send_input_event(self, serial: str, event) -> bool:
        if serial not in self.summary.opened:
            return False
        self.input_events.append((serial, event))
        return True

    async def health_tick(self) -> None:
        self.health_ticks += 1

    async def close_all(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_observer(monkeypatch):
    FakeObserver.instances = []
    FakeObserver.config = {}
    FakeObserver.fail_open = set()
    monkeypatch.setattr(nightly_soak, "SoakObserver", FakeObserver)
    yield


class Observations:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    async def __call__(self, severity, kind, message, data) -> None:
        self.items.append((severity, kind, message))

    def kinds(self) -> list[str]:
        return [k for _s, k, _m in self.items]


async def _seed_device(db, serial="S1", port="/dev/ttyUSB0", env="heltec-v3", node_num=None):
    await db.execute(
        "INSERT INTO devices (serial_number, node_num, current_port, env, online, kind) "
        "VALUES (?,?,?,?,1,'usb')",
        (serial, node_num, port, env),
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


def test_preflight_flags_default_channel_and_unset_region(tmp_path: Path):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        await _seed_device(db, "BAD", "/dev/b")
        await _seed_device(db, "NOREGION", "/dev/c")
        FakeObserver.config = {
            "GOOD": ("McpTest", "US"),
            "BAD": ("LongFast", "US"),
            "NOREGION": ("McpTest", "UNSET"),
        }
        obs = Observations()
        soak = _soak(db, tmp_path, obs)
        await soak.run(duration_s=0.0)

        kinds = obs.kinds()
        assert kinds.count("channel.default_profile") == 1
        assert kinds.count("channel.region_unset") == 1
        assert soak.summary.preflight_failures == 2
        assert soak._verified == {"GOOD"}
        bad = next(m for _s, k, m in obs.items if k == "channel.default_profile")
        assert "BAD" in bad and "LongFast" in bad
        await db.close()

    asyncio.run(go())


def test_observer_open_failure_is_preflight_failure(tmp_path: Path):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        await _seed_device(db, "DEAD", "/dev/b")
        FakeObserver.config = {"GOOD": ("McpTest", "US")}
        FakeObserver.fail_open = {"DEAD"}
        obs = Observations()
        soak = _soak(db, tmp_path, obs)
        summary = await soak.run(duration_s=0.0)

        assert "soak.preflight_failed" in obs.kinds()
        assert summary.preflight_failures == 1
        assert summary.observers_opened == ["GOOD"]
        assert "DEAD" in summary.observers_failed
        # A failed open releases its claim immediately (nothing is held) and
        # gives the monitor back — only GOOD stays claimed for the window.
        assert soak.portlocks.claims == {}
        assert ("DEAD", nightly_soak.CLAIM_OWNER) in soak.portlocks.release_log
        assert soak.serialmon.resumed.count("DEAD") == 1
        await db.close()

    asyncio.run(go())


def test_run_sends_traffic_and_snapshots(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.05)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    def fake_snapshot(device_index, *, rotation=0, mirror=False):
        return b"\xff\xd8fakejpeg"

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1", "/dev/a")
        cid = await rc.add(db, name="cam0", device_index="0")
        await rc.assign(db, cid, "S1")

        FakeObserver.config = {"S1": ("McpTest", "US")}
        monkeypatch.setattr(nightly_soak.camera_stream, "snapshot", fake_snapshot)

        obs = Observations()
        cfg = NightlyConfig(soak_traffic_interval_min=0.001, soak_snapshot_interval_min=0.001)
        soak = _soak(db, tmp_path, obs, cfg=cfg, nightly_id=9)
        summary = await soak.run(duration_s=0.4)

        observer = FakeObserver.instances[-1]
        assert summary.sends_attempted >= 1 and summary.sends_failed == 0
        assert observer.sent and all(t.startswith("nightly-9-") for _s, t in observer.sent)
        assert observer.health_ticks >= 1 and observer.closed
        sends_file = tmp_path / "night" / nightly_soak.SENDS_FILE
        rows = [json.loads(ln) for ln in sends_file.read_text().splitlines()]
        assert rows[0]["ok"] is True and rows[0]["text"] == "nightly-9-0"
        assert summary.snapshots and (tmp_path / "night" / summary.snapshots[0]).exists()
        # Port discipline: the device is claimed + monitor-suspended for the
        # window, then released + resumed at the end (even on error paths).
        assert soak.portlocks.claim_log == [("S1", nightly_soak.CLAIM_OWNER)]
        assert soak.portlocks.release_log == [("S1", nightly_soak.CLAIM_OWNER)]
        assert soak.portlocks.claims == {}
        assert soak.serialmon.suspended == ["S1"]
        assert soak.serialmon.resumed == ["S1"]
        await db.close()

    asyncio.run(go())


def test_no_transmit_from_misbaked_device(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.02)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        await _seed_device(db, "BAD", "/dev/b")
        # GOOD is on the private channel; BAD is on LongFast (misbaked).
        FakeObserver.config = {"GOOD": ("McpTest", "US"), "BAD": ("LongFast", "US")}
        obs = Observations()
        cfg = NightlyConfig(soak_traffic_interval_min=0.001)
        soak = _soak(db, tmp_path, obs, cfg=cfg, nightly_id=3)
        await soak.run(duration_s=0.3)

        # Only GOOD transmits; BAD (on LongFast) never originates traffic.
        assert soak._verified == {"GOOD"}
        assert "channel.default_profile" in obs.kinds()
        observer = FakeObserver.instances[-1]
        assert observer.sent and all(s == "GOOD" for s, _t in observer.sent)
        sends = tmp_path / "night" / nightly_soak.SENDS_FILE
        rows = [json.loads(ln) for ln in sends.read_text().splitlines()] if sends.exists() else []
        assert rows and all(r["serial"] == "GOOD" for r in rows)
        # BAD is still observed (records/capture), just never a sender.
        assert set(observer.summary.opened) == {"GOOD", "BAD"}
        await db.close()

    asyncio.run(go())


def test_send_seq_resumes_from_existing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "MIN_ACTION_PERIOD_S", 0.02)
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "GOOD", "/dev/a")
        FakeObserver.config = {"GOOD": ("McpTest", "US")}
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
        observer = FakeObserver.instances[-1]
        # Sequence continues at 3 — no id already on the wire is reused.
        assert observer.sent and observer.sent[0][1] == "nightly-5-3"
        await db.close()

    asyncio.run(go())


def test_keepalive_relay_registered_and_cleared(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nightly_soak, "_TICK_S", 0.02)

    class FakeKeepalive:
        cfg = {"enabled": True}
        relay = None
        seen: list[object] = []

    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "S1", "/dev/a")
        FakeObserver.config = {"S1": ("McpTest", "US")}
        ka = FakeKeepalive()

        orig_tick = FakeObserver.health_tick

        async def spy_tick(self) -> None:
            FakeKeepalive.seen.append(ka.relay)
            await orig_tick(self)

        monkeypatch.setattr(FakeObserver, "health_tick", spy_tick)
        soak = _soak(db, tmp_path, Observations(), keepalive=ka)
        await soak.run(duration_s=0.1)
        # The relay pointed at the observer during the loop and is cleared after.
        assert FakeKeepalive.seen and all(
            r is FakeObserver.instances[-1] for r in FakeKeepalive.seen
        )
        assert ka.relay is None
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
