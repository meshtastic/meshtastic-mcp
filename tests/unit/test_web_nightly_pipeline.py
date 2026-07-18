# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""NightlyOrchestrator pipeline: step ordering, per-step failure policy,
report-always, restart/resume decisions, cancel, and retention pruning —
against fully faked runner/builder/recovery/soak/reporter services."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_nightly as rn
from meshtastic_mcp.web.db import repo_runs as rr
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly, nightly_soak


class FakeHub:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    async def publish(self, topic: str, data: dict) -> None:
        self.frames.append((topic, data))

    def publish_threadsafe(self, topic: str, data: dict) -> None:
        self.frames.append((topic, data))


class FakeRunner:
    def __init__(self, db, run_exit=0) -> None:
        self.db = db
        self.run_exit = run_exit
        self.started_with: list[list[str]] = []
        self.stopped = False

    async def start(self, args: list[str]) -> dict:
        self.started_with.append(args)
        run_id = await rr.create_run(
            self.db, args=args, seed=None, fw_branch="develop", fw_sha="new", fw_dirty=False
        )
        await rr.finish_run(self.db, run_id, exit_code=self.run_exit)
        return {"running": False, "run_id": run_id}

    async def stop(self) -> None:
        self.stopped = True

    async def reset(self) -> None:
        pass


class FakeOrch:
    def __init__(self, fail_envs: set[str] | None = None) -> None:
        self.enqueued: list[tuple[list[str], str]] = []
        self.fail_envs = fail_envs or set()

    async def enqueue(self, envs, *, sha, branch, force=False):
        self.enqueued.append((list(envs), sha))
        return []

    async def wait(self, envs, *, sha, timeout_s=0):
        return [
            {
                "env": e,
                "status": "failed" if e in self.fail_envs else "success",
                "error": "boom" if e in self.fail_envs else None,
            }
            for e in envs
        ]


class FakeRecovery:
    def __init__(self, revives=True) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.revives = revives

    async def recover(self, serial, *, allow_reflash=False, confirm=False, steps=None):
        self.calls.append((serial, allow_reflash))
        return {"recovered": self.revives}


class FakeReporter:
    def __init__(self) -> None:
        self.published: list[int] = []

    async def publish(self, nightly_id: int):
        self.published.append(nightly_id)
        return {"status": "posted"}


class FakeSerialMon:
    def __init__(self) -> None:
        self.sinks: list = []

    def is_wedged(self, serial: str) -> bool:
        return False


class FakeSoak:
    instances: list = []

    def __init__(self, *a, **kw) -> None:
        self.kw = kw
        FakeSoak.instances.append(self)

    async def run(self, duration_s, cancel=None):
        self.duration_s = duration_s
        return nightly_soak.SoakSummary(started_at=1.0, ended_at=2.0, lines={"S1": 3})


@pytest.fixture()
def quick(monkeypatch):
    """Shrink every wait so pipelines complete in milliseconds."""
    monkeypatch.setattr(nightly, "GATE_GRACE_S", 0.2)
    monkeypatch.setattr(nightly, "GATE_POLL_S", 0.02)
    monkeypatch.setattr(nightly, "SUITE_POLL_S", 0.02)
    monkeypatch.setattr(nightly, "RESTART_EXIT_WAIT_S", 0.05)
    monkeypatch.setattr(nightly, "RECOVERY_SETTLE_S", 0.0)
    monkeypatch.setattr(nightly.tr_mod, "is_running", lambda: False)
    monkeypatch.setattr(nightly.tr_mod, "status", lambda: {"run_id": None})
    monkeypatch.setattr(nightly, "mcp_source_root", lambda: None)
    monkeypatch.setattr(
        nightly,
        "firmware_update",
        lambda d, *, url, branch, runner=None: nightly.FwUpdateResult(
            ok=True, sha_before="old", sha_after="new"
        ),
    )
    monkeypatch.setattr(nightly_soak, "NightlySoak", FakeSoak)
    FakeSoak.instances = []


async def _make(db, tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(nightly, "nightly_data_dir", lambda nid: tmp_path / "nights" / str(nid))
    hub = FakeHub()
    orch = nightly.NightlyOrchestrator(
        db,
        hub,
        runner=kw.get("runner") or FakeRunner(db),
        orch=kw.get("orch") or FakeOrch(),
        serialmon=kw.get("serialmon") or FakeSerialMon(),
        portlocks=None,
        recovery=kw.get("recovery", FakeRecovery()),
        reporter=kw.get("reporter") or FakeReporter(),
    )
    orch.cfg = kw.get("cfg") or nightly.NightlyConfig(enabled=True, soak_hours=1.0)
    return orch, hub


async def _seed_device(db, serial="S1", *, online=1, env="heltec-v3"):
    await db.execute(
        "INSERT INTO devices (serial_number, current_port, env, online, kind) "
        "VALUES (?,?,?,?,'usb')",
        (serial, "/dev/a", env, online),
    )


async def _run_night(orch) -> int:
    res = await orch.run_now()
    assert orch._pipeline_task is not None
    await orch._pipeline_task
    return res["nightly_id"]


def _kinds(obs: list[dict]) -> list[str]:
    return [o["kind"] for o in obs]


def test_happy_path_order_and_report(tmp_path: Path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db)
        reporter = FakeReporter()
        runner = FakeRunner(db)
        orch, hub = await _make(db, tmp_path, monkeypatch, reporter=reporter, runner=runner)
        nid = await _run_night(orch)

        row = await rn.get(db, nid)
        assert row is not None
        assert row["status"] == "passed" and row["step"] == "done"
        assert row["fw_sha_before"] == "old" and row["fw_sha_after"] == "new"
        assert row["run_id"] is not None
        assert row["summary"] == {"passed": 0, "failed": 0, "skipped": 0, "exit_code": 0}
        assert reporter.published == [nid]
        assert runner.started_with == [["--force-bake"]]
        # Steps advanced in order (via the WS step frames).
        steps = [d["step"] for t, d in hub.frames if d.get("type") == "step"]
        assert steps == [
            "self_update",
            "firmware_update",
            "prebuild",
            "bench_check",
            "suite",
            "soak",
            "bench_recover",
            "handoff",
        ]
        # Soak got the configured window and its summary was recorded.
        assert FakeSoak.instances and FakeSoak.instances[0].duration_s == pytest.approx(3600.0)
        obs = await rn.observations(db, nid)
        assert "soak.summary" in _kinds(obs)
        await db.close()

    asyncio.run(go())


def test_firmware_clone_failure_skips_suite_but_reports(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(
        nightly,
        "firmware_update",
        lambda d, *, url, branch, runner=None: nightly.FwUpdateResult(
            ok=False, cloned=True, error="disk full"
        ),
    )

    async def go():
        db = await Database(tmp_path / "db").connect()
        reporter = FakeReporter()
        runner = FakeRunner(db)
        orch, _hub = await _make(db, tmp_path, monkeypatch, reporter=reporter, runner=runner)
        nid = await _run_night(orch)

        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "error"
        assert runner.started_with == []  # suite never launched
        assert reporter.published == [nid]  # report-always
        obs = await rn.observations(db, nid)
        assert "firmware.clone_failed" in _kinds(obs)
        await db.close()

    asyncio.run(go())


def test_fetch_failure_still_runs_suite_on_old_sha(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(
        nightly,
        "firmware_update",
        lambda d, *, url, branch, runner=None: nightly.FwUpdateResult(
            ok=False, cloned=False, sha_before="old", error="network down"
        ),
    )

    async def go():
        db = await Database(tmp_path / "db").connect()
        runner = FakeRunner(db)
        orch, _hub = await _make(db, tmp_path, monkeypatch, runner=runner)
        nid = await _run_night(orch)
        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "passed"
        assert row["fw_sha_after"] == "old"  # ran on the existing checkout
        assert runner.started_with  # suite DID run
        obs = await rn.observations(db, nid)
        assert "firmware.fetch_failed" in _kinds(obs)
        await db.close()

    asyncio.run(go())


def test_prebuild_failure_never_gates(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, env="rak4631")
        runner = FakeRunner(db)
        orch, _hub = await _make(
            db, tmp_path, monkeypatch, runner=runner, orch=FakeOrch(fail_envs={"rak4631"})
        )
        nid = await _run_night(orch)
        obs = await rn.observations(db, nid)
        assert "build.failed" in _kinds(obs)
        assert runner.started_with  # suite still ran
        await db.close()

    asyncio.run(go())


def test_suite_failure_marks_night_failed(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        orch, _hub = await _make(db, tmp_path, monkeypatch, runner=FakeRunner(db, run_exit=1))
        nid = await _run_night(orch)
        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "failed"
        await db.close()

    asyncio.run(go())


def test_bench_check_and_recover_use_ladder(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        await _seed_device(db, "DEAD", online=0)
        recovery = FakeRecovery(revives=False)
        orch, _hub = await _make(db, tmp_path, monkeypatch, recovery=recovery)
        nid = await _run_night(orch)
        # bench_check ran SAFE (reflash off); bench_recover escalated (reflash on).
        assert (("DEAD", False) in recovery.calls) and (("DEAD", True) in recovery.calls)
        obs = await rn.observations(db, nid)
        assert "recovery.attempted" in _kinds(obs) and "recovery.failed" in _kinds(obs)
        await db.close()

    asyncio.run(go())


def test_self_update_restart_persists_awaiting_restart(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(nightly, "mcp_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        nightly,
        "self_update",
        lambda root, runner=None, python=None: nightly.SelfUpdateResult(
            ok=True, updated=True, sha_before="m1", sha_after="m2"
        ),
    )
    kills: list[int] = []

    async def go():
        db = await Database(tmp_path / "db").connect()
        orch, _hub = await _make(db, tmp_path, monkeypatch)

        def fake_kill(pid, sig):
            kills.append(sig)

        monkeypatch.setattr(nightly.os, "kill", fake_kill)
        nid = await _run_night(orch)

        assert kills  # SIGTERM was sent
        row = await rn.get(db, nid)
        assert row is not None
        obs = await rn.observations(db, nid)
        kinds = _kinds(obs)
        # Process survived the fake kill → restart_failed, night continued.
        assert "self_update.restarting" in kinds and "self_update.restart_failed" in kinds
        assert row["mcp_sha_before"] == "m1" and row["mcp_sha_after"] == "m2"
        assert row["status"] in ("passed", "failed")
        await db.close()

    asyncio.run(go())


def test_resume_decision_table(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        orch, _hub = await _make(db, tmp_path, monkeypatch)
        launches: list[tuple[int, str | None]] = []
        monkeypatch.setattr(
            orch, "_launch", lambda nid, resume_from: launches.append((nid, resume_from))
        )

        # awaiting_restart → resume at firmware_update, status flips to running.
        n1 = await rn.create(db, scheduled_for=1.0)
        await rn.set_status(db, n1, "awaiting_restart")
        assert await orch._maybe_resume()
        assert launches[-1] == (n1, "firmware_update")
        assert (await rn.get(db, n1))["status"] == "running"
        await rn.finish(db, n1, status="passed", summary=None)

        # running@prebuild → idempotent re-run of that step.
        n2 = await rn.create(db, scheduled_for=2.0)
        await rn.set_step(db, n2, "prebuild")
        assert await orch._maybe_resume()
        assert launches[-1] == (n2, "prebuild")
        await rn.finish(db, n2, status="passed", summary=None)

        # running@suite, first death → orphan run closed, suite retried.
        n3 = await rn.create(db, scheduled_for=3.0)
        await rn.set_step(db, n3, "suite")
        run_id = await rr.create_run(
            db, args=[], seed=None, fw_branch=None, fw_sha=None, fw_dirty=False
        )
        await rn.set_run_id(db, n3, run_id)
        assert await orch._maybe_resume()
        assert launches[-1] == (n3, "suite")
        assert (await rr.get_run(db, run_id))["finished_at"] is not None  # orphan closed
        # Second death → crash-loop guard sends it straight to handoff.
        assert await orch._maybe_resume()
        assert launches[-1] == (n3, "handoff")
        obs = await rn.observations(db, n3)
        assert "suite.crash_loop" in _kinds(obs)
        await rn.finish(db, n3, status="error", summary=None)

        assert not await orch._maybe_resume()  # nothing unfinished left
        await db.close()

    asyncio.run(go())


def test_cancel_marks_canceled_and_still_reports(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        reporter = FakeReporter()

        class SlowSoak(FakeSoak):
            async def run(self, duration_s, cancel=None):
                while cancel is not None and not cancel.is_set():
                    await asyncio.sleep(0.01)
                return nightly_soak.SoakSummary(started_at=1.0, ended_at=2.0)

        monkeypatch.setattr(nightly_soak, "NightlySoak", SlowSoak)
        orch, _hub = await _make(db, tmp_path, monkeypatch, reporter=reporter)
        res = await orch.run_now()
        # Let the pipeline reach the soak, then cancel.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if orch._current_step == "soak":
                break
        await orch.cancel()
        await orch._pipeline_task
        row = await rn.get(db, res["nightly_id"])
        assert row is not None and row["status"] == "canceled"
        assert reporter.published == [res["nightly_id"]]
        await db.close()

    asyncio.run(go())


def test_gate_bench_busy_aborts_to_report(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(nightly.tr_mod, "is_running", lambda: True)
    monkeypatch.setattr(
        nightly.tr_mod, "status", lambda: {"run_id": 1, "elapsed_s": 5.0, "last_line": "..."}
    )

    async def go():
        db = await Database(tmp_path / "db").connect()
        reporter = FakeReporter()
        orch, _hub = await _make(db, tmp_path, monkeypatch, reporter=reporter)
        nid = await _run_night(orch)
        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "error"
        obs = await rn.observations(db, nid)
        assert "bench.busy" in _kinds(obs)
        assert reporter.published == [nid]
        await db.close()

    asyncio.run(go())


def test_double_launch_rejected(tmp_path, monkeypatch, quick):
    async def go():
        db = await Database(tmp_path / "db").connect()
        orch, _hub = await _make(db, tmp_path, monkeypatch)
        # Two concurrent run_now() calls: the launch lock must let exactly one
        # through and 409 the other, with no orphaned second pipeline row.
        results = await asyncio.gather(orch.run_now(), orch.run_now(), return_exceptions=True)
        oks = [r for r in results if isinstance(r, dict)]
        errs = [r for r in results if isinstance(r, RuntimeError)]
        assert len(oks) == 1 and len(errs) == 1
        await orch._pipeline_task
        assert len(await rn.list_runs(db)) == 1
        await db.close()

    asyncio.run(go())


def test_cancel_during_gate_spares_manual_run(tmp_path, monkeypatch, quick):
    # The gate is where a manual run holds the bench. Cancelling there must set
    # the flag but NOT stop the runner (that would kill the operator's run).
    monkeypatch.setattr(nightly.tr_mod, "is_running", lambda: True)
    monkeypatch.setattr(
        nightly.tr_mod, "status", lambda: {"run_id": 1, "elapsed_s": 5.0, "last_line": "..."}
    )

    async def go():
        db = await Database(tmp_path / "db").connect()
        runner = FakeRunner(db)
        orch, _hub = await _make(db, tmp_path, monkeypatch, runner=runner)
        res = await orch.run_now()
        # Let it reach the gate loop.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if orch._current_step is None and orch.is_pipeline_active():
                break
        await orch.cancel()
        await orch._pipeline_task
        assert runner.stopped is False  # manual run left untouched
        row = await rn.get(db, res["nightly_id"])
        assert row is not None and row["status"] in ("canceled", "error")
        await db.close()

    asyncio.run(go())


def test_resume_at_finalize_skips_pipeline_rerun(tmp_path, monkeypatch, quick):
    # A crash during bench_recover/handoff must resume straight into finalize
    # (report-always) without re-running self_update/firmware/suite.
    async def go():
        db = await Database(tmp_path / "db").connect()
        reporter = FakeReporter()
        runner = FakeRunner(db)
        orch, _hub = await _make(db, tmp_path, monkeypatch, reporter=reporter, runner=runner)
        # Seed an unfinished night stuck at bench_recover with a failed run.
        nid = await rn.create(db, scheduled_for=1.0)
        run_id = await rr.create_run(
            db, args=[], seed=None, fw_branch="develop", fw_sha="x", fw_dirty=False
        )
        await rr.finish_run(db, run_id, exit_code=1)
        await rn.set_run_id(db, nid, run_id)
        await rn.set_step(db, nid, "bench_recover")

        assert await orch._maybe_resume()
        await orch._pipeline_task
        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "failed"  # recomputed, not green
        assert reporter.published == [nid]  # report still delivered
        assert runner.started_with == []  # suite NOT re-run
        await db.close()

    asyncio.run(go())


def test_retention_prune(tmp_path, monkeypatch, quick):
    monkeypatch.setenv("MESHTASTIC_MCP_ARTIFACT_DIR", str(tmp_path / "artifacts"))

    async def go():
        db = await Database(tmp_path / "db").connect()
        orch, _hub = await _make(db, tmp_path, monkeypatch)
        orch.cfg.keep_nights = 2

        old_ids = []
        for i, sha in enumerate(("oldsha1", "oldsha2", "keepsha", "keepsha")):
            nid = await rn.create(db, scheduled_for=float(i))
            await rn.set_shas(db, nid, fw_after=sha)
            await rn.finish(db, nid, status="passed", summary=None)
            old_ids.append(nid)
            (tmp_path / "nights" / str(nid)).mkdir(parents=True, exist_ok=True)
        for sha in ("oldsha1", "oldsha2", "keepsha"):
            (tmp_path / "artifacts" / sha / "env").mkdir(parents=True, exist_ok=True)

        await orch._retention_prune(old_ids[-1])

        # The two oldest night dirs are gone; the two newest stay.
        assert not (tmp_path / "nights" / str(old_ids[0])).exists()
        assert not (tmp_path / "nights" / str(old_ids[1])).exists()
        assert (tmp_path / "nights" / str(old_ids[2])).exists()
        # Artifact trees for old-only shas pruned; the recent sha survives.
        assert not (tmp_path / "artifacts" / "oldsha1").exists()
        assert not (tmp_path / "artifacts" / "oldsha2").exists()
        assert (tmp_path / "artifacts" / "keepsha").exists()
        await db.close()

    asyncio.run(go())
