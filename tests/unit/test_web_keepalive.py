# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Screen keep-alive provisioning logic.

Drives the async ``ScreenKeepAlive`` service via ``asyncio.run`` against a
tmpdir-backed Database, with the device-admin calls stubbed — no hardware, no
event loop fixtures. Verifies the contract FleetSuite's screen keep-alive
promises: provision ``display.screen_on_secs`` exactly once per device, then
poke each online node with an input-broker event every cycle, and never touch a
port while a test run owns it.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_devices as rd
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import keepalive as ka_mod
from meshtastic_mcp.web.services.keepalive import ScreenKeepAlive


class _Hub:
    async def publish(self, *a, **k) -> None:
        pass


def _fresh_db(tmp_path) -> Database:
    return Database(path=tmp_path / "keepalive.db")


async def _add_esp(db: Database, serial: str, port: str) -> None:
    await rd.upsert_from_discovery(
        db,
        serial_number=serial,
        current_port=port,
        vid="0x10c4",
        pid="0x1",
        role="esp32s3",
    )


def test_provisions_once_then_pokes_each_cycle(tmp_path, monkeypatch):
    set_calls: list = []
    input_calls: list = []
    monkeypatch.setattr(
        "meshtastic_mcp.admin.set_config",
        lambda path, value, port: set_calls.append((path, value, port)) or {"ok": True},
    )
    monkeypatch.setattr(
        "meshtastic_mcp.admin.send_input_event",
        lambda event_code, kb=0, tx=0, ty=0, port=None: (
            input_calls.append((event_code, port)) or {"ok": True}
        ),
    )
    monkeypatch.setattr("meshtastic_mcp.web.services.test_runner.is_running", lambda: False)

    async def go():
        db = _fresh_db(tmp_path)
        await db.connect()
        try:
            await _add_esp(db, "ESP", "/dev/cu.x")
            ka = ScreenKeepAlive(db, _Hub())
            ka.cfg["enabled"] = True

            await ka._cycle()  # first pass: provision + poke
            await ka._cycle()  # second pass: poke only (no re-provision)

            # screen_on_secs written exactly once, with the configured value.
            assert set_calls == [
                (
                    "display.screen_on_secs",
                    ka_mod.DEFAULTS["screen_on_secs"],
                    "/dev/cu.x",
                )
            ]
            # input event sent every cycle, to the device's current port.
            assert [port for _, port in input_calls] == ["/dev/cu.x", "/dev/cu.x"]
            assert {ev for ev, _ in input_calls} == {ka_mod.DEFAULTS["event"]}
            assert ka.stats["provisioned"] == 1
            assert ka.stats["events_sent"] == 2
            assert ka.stats["last_error"] is None
        finally:
            await db.close()

    asyncio.run(go())


def test_skips_every_action_while_a_run_owns_the_ports(tmp_path, monkeypatch):
    touched: list = []
    monkeypatch.setattr("meshtastic_mcp.admin.set_config", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "meshtastic_mcp.admin.send_input_event",
        lambda *a, port=None, **k: touched.append(port) or {"ok": True},
    )
    monkeypatch.setattr("meshtastic_mcp.web.services.test_runner.is_running", lambda: True)

    async def go():
        db = _fresh_db(tmp_path)
        await db.connect()
        try:
            await _add_esp(db, "ESP", "/dev/x")
            ka = ScreenKeepAlive(db, _Hub())
            ka.cfg["enabled"] = True
            await ka._cycle()
            assert touched == []  # the runner owns the ports — hands off
        finally:
            await db.close()

    asyncio.run(go())


def test_disabled_does_nothing(tmp_path, monkeypatch):
    touched: list = []
    monkeypatch.setattr(
        "meshtastic_mcp.admin.send_input_event",
        lambda *a, **k: touched.append(1) or {"ok": True},
    )
    monkeypatch.setattr("meshtastic_mcp.web.services.test_runner.is_running", lambda: False)

    async def go():
        db = _fresh_db(tmp_path)
        await db.connect()
        try:
            await _add_esp(db, "ESP", "/dev/x")
            ka = ScreenKeepAlive(db, _Hub())  # enabled defaults to False
            await ka._cycle()
            assert touched == []
        finally:
            await db.close()

    asyncio.run(go())


def test_config_round_trips_through_settings(tmp_path):
    async def go():
        db = _fresh_db(tmp_path)
        await db.connect()
        try:
            await ScreenKeepAlive(db, _Hub()).save(
                {"enabled": True, "interval_s": 15, "event": "RIGHT", "bogus": 1}
            )
            ka2 = ScreenKeepAlive(db, _Hub())
            await ka2.reload()
            assert ka2.cfg["enabled"] is True
            assert ka2.cfg["interval_s"] == 15
            assert ka2.cfg["event"] == "RIGHT"
            assert "bogus" not in ka2.cfg  # unknown keys dropped
            # untouched keys keep their defaults
            assert ka2.cfg["screen_on_secs"] == ka_mod.DEFAULTS["screen_on_secs"]
        finally:
            await db.close()

    asyncio.run(go())
