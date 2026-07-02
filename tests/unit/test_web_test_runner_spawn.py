# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The pytest launch must not wedge in FleetSuite desktop mode.

In desktop mode the server runs pywebview (Cocoa/WebKit) on the main thread and
uvicorn/asyncio on a daemon thread. Launching pytest with ``cwd`` set forces
CPython's fork+exec path, and forking a CoreFoundation/Objective-C-initialised
process deadlocks on macOS — pytest never starts and the run hangs at
``running:true`` forever. ``build_pytest_invocation`` avoids this by spawning via
``/bin/sh`` with ``cwd=None`` (so CPython uses ``posix_spawn`` — no fork) on
darwin. These tests lock that behaviour and prove a real pytest launches through
the chosen command.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services import test_runner as tr
from meshtastic_mcp.web.services.test_runner import build_pytest_invocation


def test_darwin_uses_posix_spawn_via_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    report = tmp_path / "report.jsonl"
    cmd, cwd, env_extra = build_pytest_invocation(report, ["tests/unit"], tmp_path)

    # cwd=None is what lets CPython pick posix_spawn over fork+exec.
    assert cwd is None
    assert cmd[0] == "/bin/sh" and cmd[1] == "-c"
    # The shell chdirs into the project and exec's pytest so the tracked process
    # *is* pytest (terminate/kill hit it directly).
    assert cmd[2].startswith(f"cd {tmp_path}")
    assert " && exec " in cmd[2]
    assert "-m pytest" in cmd[2] and "tests/unit" in cmd[2]
    # Belt-and-suspenders: disable the ObjC fork-safety abort.
    assert env_extra.get("OBJC_DISABLE_INITIALIZE_FORK_SAFETY") == "YES"


def test_non_darwin_runs_pytest_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    report = tmp_path / "report.jsonl"
    cmd, cwd, env_extra = build_pytest_invocation(report, ["-m", "unit"], tmp_path)

    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "pytest"]
    assert cmd[-2:] == ["-m", "unit"]
    assert cwd == tmp_path
    assert env_extra == {}


def test_drive_always_resets_running_on_setup_error(tmp_path, monkeypatch):
    """Regression: a stray exception in _drive's setup (the original bug was a
    bad import: `from .. import config`) used to escape before the try/finally
    and wedge the run at running:true forever. _drive must ALWAYS reset the run
    flag — even if setup blows up — so the UI can never get stuck."""
    from meshtastic_mcp.web.db import repo_runs as rr
    from meshtastic_mcp.web.db.database import Database
    from meshtastic_mcp.web.ws.hub import Hub

    # Make the spawn setup explode the way a bad import / bad env would.
    monkeypatch.setattr(
        tr,
        "build_pytest_invocation",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    async def go():
        db = Database(path=tmp_path / "reg.db")
        await db.connect()
        hub = Hub()
        hub.bind_loop(asyncio.get_running_loop())
        runner = tr.TestRunner(db, hub, serialmon=None)
        run_id = await rr.create_run(
            db, args=[], seed="t", fw_branch=None, fw_sha=None, fw_dirty=False
        )
        tr._state.update(running=True, run_id=run_id, exit_code=None)
        # _drive must swallow the error and finish the run, not raise/wedge.
        await runner._drive(run_id, [], {})
        assert tr.is_running() is False, "run flag left stuck after a setup error"
        assert tr.status()["exit_code"] == 1
        row = await rr.get_run(db, run_id) if hasattr(rr, "get_run") else None
        if row is not None:
            assert row.get("finished_at") is not None
        await db.close()

    try:
        asyncio.run(go())
    finally:
        tr._state.update(running=False, run_id=None, exit_code=None, proc=None)


def test_drive_runs_pytest_and_finishes(tmp_path, monkeypatch):
    """Happy path through the real _drive: a trivial pytest run completes,
    records a result, and resets running. This exercises the exact path that
    was wedging (start→_drive→spawn→reportlog→finish)."""
    from meshtastic_mcp.web.db import repo_runs as rr
    from meshtastic_mcp.web.db.database import Database
    from meshtastic_mcp.web.ws.hub import Hub

    (tmp_path / "test_trivial.py").write_text("def test_ok():\n    assert True\n")
    # _drive cds into `_repo_root()`; point it at our tmp tree so pytest collects
    # only our trivial test (not the real repo suite).
    monkeypatch.setattr(tr, "_repo_root", lambda: tmp_path)

    async def go():
        db = Database(path=tmp_path / "reg.db")
        await db.connect()
        hub = Hub()
        hub.bind_loop(asyncio.get_running_loop())
        runner = tr.TestRunner(db, hub, serialmon=None)
        run_id = await rr.create_run(
            db, args=[], seed="t", fw_branch=None, fw_sha=None, fw_dirty=False
        )
        tr._state.update(running=True, run_id=run_id, exit_code=None)
        await asyncio.wait_for(
            runner._drive(run_id, ["-p", "no:cacheprovider", "test_trivial.py"], {}),
            timeout=120,
        )
        assert tr.is_running() is False
        assert tr.status()["exit_code"] == 0, "trivial pytest run did not pass"
        await db.close()

    try:
        asyncio.run(go())
    finally:
        tr._state.update(running=False, run_id=None, exit_code=None, proc=None)


def test_in_flight_state_and_heartbeat(monkeypatch):
    """A setup report marks the test in-flight (so status() + the heartbeat report
    the current test + elapsed + last line) — the liveness the UI needs so a long
    test never reads as 'stuck'."""
    monkeypatch.setattr(tr, "HEARTBEAT_S", 0.01)
    frames: list = []

    class RecHub:
        async def publish(self, topic, data):
            frames.append((topic, data))

    runner = tr.TestRunner(db=None, hub=RecHub(), serialmon=None)

    async def go():
        tr._state.update(running=True, nodeid=None, since=None, last_line=None)
        await runner._handle_entry(
            7,
            {
                "$report_type": "TestReport",
                "nodeid": "tests/test_00_bake.py::test_bake_esp32s3",
                "when": "setup",
                "outcome": "passed",
            },
            set(),
            None,
        )
        assert tr._state["nodeid"] == "tests/test_00_bake.py::test_bake_esp32s3"
        st = tr.status()
        assert st["nodeid"] == "tests/test_00_bake.py::test_bake_esp32s3"
        assert st["elapsed_s"] is not None

        tr._state["last_line"] = "Compiling .pio/build/heltec-v3/src/main.cpp.o"
        hb = asyncio.create_task(runner._heartbeat())
        await asyncio.sleep(0.05)
        tr._state["running"] = False
        await asyncio.sleep(0.03)
        hb.cancel()

        beats = [d for t, d in frames if t == "test.progress" and d.get("type") == "heartbeat"]
        assert beats, "no heartbeat emitted for the in-flight test"
        assert beats[-1]["nodeid"] == "tests/test_00_bake.py::test_bake_esp32s3"
        assert beats[-1]["last_line"] == "Compiling .pio/build/heltec-v3/src/main.cpp.o"

    try:
        asyncio.run(go())
    finally:
        tr._state.update(
            running=False,
            run_id=None,
            exit_code=None,
            proc=None,
            nodeid=None,
            since=None,
            last_line=None,
        )


def test_results_pipeline_registers_and_survives_null_collectreport():
    """The reportlog→UI pipeline that drives the tier counters. Two regressions:
    (1) a CollectReport with result=None must NOT raise (it used to crash the
    whole tail task on the first collect report, freezing every tier at 0/0/0);
    (2) a TestReport 'setup' must publish a 'register' frame (CollectReport.result
    is empty in this reportlog version, so setup is the only reliable source of
    test nodeids — without it no leaf exists and nothing counts)."""
    frames: list = []

    class RecHub:
        async def publish(self, topic, data):
            frames.append((topic, data))

    class FakeRR:
        async def add_result(self, *a, **k):
            pass

    runner = tr.TestRunner(db=None, hub=RecHub(), serialmon=None)
    seen: set = set()
    rr = FakeRR()
    nid = "tests/mesh/test_x.py::test_y"

    async def go():
        # result=None used to blow up `for item in entry.get("result", [])`.
        await runner._handle_entry(
            1, {"$report_type": "CollectReport", "nodeid": "scripts", "result": None}, seen, rr
        )
        await runner._handle_entry(
            1,
            {"$report_type": "TestReport", "nodeid": nid, "when": "setup", "outcome": "passed"},
            seen,
            rr,
        )
        await runner._handle_entry(
            1,
            {
                "$report_type": "TestReport",
                "nodeid": nid,
                "when": "call",
                "outcome": "failed",
                "duration": 1.2,
            },
            seen,
            rr,
        )

    try:
        asyncio.run(go())
    finally:
        tr._state.update(running=False, nodeid=None, since=None, last_line=None)

    progress = [d for t, d in frames if t == "test.progress"]
    reg = [d for d in progress if d.get("type") == "register"]
    assert reg, (
        f"no register frame — tiers would stay 0/0/0. Got: {[d.get('type') for d in progress]}"
    )
    assert reg[0]["nodeid"] == nid and reg[0]["tier"] == "mesh"
    outc = [d for d in progress if d.get("type") == "outcome"]
    assert outc and outc[0]["outcome"] == "failed"


def test_bake_pass_records_flashed(tmp_path):
    """A passing `test_bake[<role>]` stamps the board on that bench slot as
    provisioned (flashed_at + branch/sha from the run) and broadcasts a
    device.update — the signal the device card uses to tell a baked board from
    one that's merely online. The board is matched by hub slot, so it works even
    for the three identical-VID nRF52 boards. A skipped/non-bake test must NOT
    stamp anything."""
    from meshtastic_mcp.web.db import repo_devices as rd
    from meshtastic_mcp.web.db import repo_runs as rr
    from meshtastic_mcp.web.db.database import Database
    from tests import _bench

    frames: list = []

    class RecHub:
        async def publish(self, topic, data):
            frames.append((topic, data))

    async def go():
        db = Database(path=tmp_path / "reg.db")
        await db.connect()
        try:
            # A rak4631 board (one of three 0x239a boards) on its bench slot.
            await rd.upsert_from_discovery(
                db,
                serial_number="RAK",
                current_port="/dev/a",
                vid="0x239a",
                pid="0x1",
                role="nrf52",
            )
            hub, port = _bench.location_hub_port(_bench.role_location("rak4631"))
            await rd.set_hub_port(db, "RAK", location=hub, port=port)
            run_id = await rr.create_run(
                db,
                args=[],
                seed="s",
                fw_branch="develop",
                fw_sha="deadbeef",
                fw_dirty=False,
            )
            runner = tr.TestRunner(db=db, hub=RecHub(), serialmon=None)
            nid = "tests/test_00_bake.py::test_bake[rak4631]"
            await runner._handle_entry(
                run_id,
                {"$report_type": "TestReport", "nodeid": nid, "when": "setup", "outcome": "passed"},
                set(),
                rr,
            )
            await runner._handle_entry(
                run_id,
                {
                    "$report_type": "TestReport",
                    "nodeid": nid,
                    "when": "call",
                    "outcome": "passed",
                    "duration": 3.0,
                },
                set(),
                rr,
            )

            row = await rd.get(db, "RAK")
            assert row["flashed_at"] is not None, "bake pass did not stamp flashed_at"
            assert row["flashed_fw_sha"] == "deadbeef"
            assert row["flashed_fw_branch"] == "develop"
            updates = [d for t, d in frames if t == "device.update"]
            assert any(d.get("serial_number") == "RAK" for d in updates), (
                "no device.update broadcast for the freshly-baked board"
            )

            # A skipped bake (already-baked → setup/skipped) must NOT re-stamp.
            await rd.upsert_from_discovery(
                db,
                serial_number="TECHO",
                current_port="/dev/b",
                vid="0x239a",
                pid="0x1",
                role="nrf52",
            )
            thub, tport = _bench.location_hub_port(_bench.role_location("t_echo"))
            await rd.set_hub_port(db, "TECHO", location=thub, port=tport)
            await runner._handle_entry(
                run_id,
                {
                    "$report_type": "TestReport",
                    "nodeid": "tests/test_00_bake.py::test_bake[t_echo]",
                    "when": "setup",
                    "outcome": "skipped",
                },
                set(),
                rr,
            )
            assert (await rd.get(db, "TECHO"))["flashed_at"] is None, (
                "a skipped bake must not mark the board provisioned"
            )
        finally:
            await db.close()

    try:
        asyncio.run(go())
    finally:
        tr._state.update(
            running=False,
            run_id=None,
            exit_code=None,
            proc=None,
            nodeid=None,
            since=None,
            last_line=None,
        )


def test_invocation_actually_launches_pytest(tmp_path):
    """End-to-end on the real platform: the command build_pytest_invocation
    produces must spawn a working pytest that writes the reportlog — i.e. the
    launch does not wedge. This is the exact spawn path _drive uses."""
    (tmp_path / "test_trivial.py").write_text("def test_ok():\n    assert True\n")
    report = tmp_path / "report.jsonl"
    cmd, cwd, env_extra = build_pytest_invocation(
        report, [str(tmp_path / "test_trivial.py")], tmp_path
    )

    async def go() -> int:
        import os

        env = {**os.environ, **env_extra}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=(str(cwd) if cwd else None),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Bound it: a wedged launch would hang here, which is exactly the bug —
        # so the test would fail (timeout) rather than hang the suite forever.
        await asyncio.wait_for(proc.communicate(), timeout=120)
        return await proc.wait()

    rc = asyncio.run(go())
    assert rc == 0, "pytest launch did not complete cleanly"
    assert report.exists() and report.stat().st_size > 0, "pytest wrote no reportlog"
    assert Path(report).read_text().find("test_ok") != -1
