# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly bake plumbing: repo CRUD, config round-trip, and the pure schedule
math that decides when a slot is due (including restart catch-up)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_nightly as rn
from meshtastic_mcp.web.db import repo_runs as rr
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly

TZ = timezone(timedelta(hours=-5))


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 18, hour, minute, tzinfo=TZ)


# --- schedule math ----------------------------------------------------------


def test_slot_for_today_and_yesterday():
    # After the slot time → today's slot.
    slot = nightly.slot_for(_at(3, 0), 1, 30)
    assert (slot.hour, slot.minute, slot.day) == (1, 30, 18)
    # Before the slot time → yesterday's slot.
    slot = nightly.slot_for(_at(0, 45), 1, 30)
    assert (slot.hour, slot.minute, slot.day) == (1, 30, 17)


def test_next_run_at_rolls_to_tomorrow():
    nxt = nightly.next_run_at(_at(2, 0), 1, 30)
    assert (nxt.day, nxt.hour, nxt.minute) == (19, 1, 30)
    nxt = nightly.next_run_at(_at(0, 0), 1, 30)
    assert (nxt.day, nxt.hour, nxt.minute) == (18, 1, 30)


def test_due_slot_within_catchup_window():
    cfg = nightly.NightlyConfig(hour=1, minute=30, catchup_window_h=6.0)
    # 3:00 — 1.5h late, within the window, never attempted → due.
    assert nightly.due_slot(_at(3, 0), cfg, None) == _at(1, 30).timestamp()
    # 9:00 — 7.5h late, outside the window → not due.
    assert nightly.due_slot(_at(9, 0), cfg, None) is None


def test_due_slot_skips_already_attempted():
    cfg = nightly.NightlyConfig(hour=1, minute=30)
    slot_ts = _at(1, 30).timestamp()
    assert nightly.due_slot(_at(3, 0), cfg, slot_ts) is None
    # An older attempt does not mask tonight's slot.
    yesterday = slot_ts - 86400
    assert nightly.due_slot(_at(3, 0), cfg, yesterday) == slot_ts


# --- config -----------------------------------------------------------------


def test_config_round_trip_and_unknown_keys(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        cfg = nightly.NightlyConfig(enabled=True, hour=3, suite_args=["-k", "smoke"])
        await nightly.save_config(db, cfg)
        loaded = await nightly.load_config(db)
        assert loaded == cfg
        await db.close()

    asyncio.run(go())
    # Unknown keys from a newer/older schema are dropped, not fatal.
    cfg = nightly.NightlyConfig.from_dict({"enabled": True, "bogus": 1})
    assert cfg.enabled is True


# --- repo_nightly CRUD ------------------------------------------------------


def test_nightly_run_lifecycle(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()

        nid = await rn.create(db, scheduled_for=1000.0)
        assert await rn.last_scheduled_for(db) == 1000.0
        row = await rn.get(db, nid)
        assert row is not None and row["status"] == "running" and row["trigger"] == "schedule"

        await rn.set_step(db, nid, "suite")
        await rn.set_run_id(db, nid, 42)
        assert await rn.bump_suite_attempts(db, nid) == 1
        await rn.set_soak_started(db, nid, 2000.0)
        await rn.set_shas(db, nid, fw_before="aaa", fw_after="bbb")
        await rn.set_shas(db, nid, mcp_before="ccc")

        unfinished = await rn.latest_unfinished(db)
        assert unfinished is not None and unfinished["id"] == nid
        assert unfinished["step"] == "suite" and unfinished["run_id"] == 42
        assert unfinished["fw_sha_before"] == "aaa" and unfinished["fw_sha_after"] == "bbb"
        assert unfinished["mcp_sha_before"] == "ccc" and unfinished["mcp_sha_after"] is None

        await rn.finish(db, nid, status="passed", summary={"passed": 10})
        assert await rn.latest_unfinished(db) is None
        row = await rn.get(db, nid)
        assert row is not None and row["summary"] == {"passed": 10}

        runs = await rn.list_runs(db)
        assert [r["id"] for r in runs] == [nid]
        await db.close()

    asyncio.run(go())


def test_last_scheduled_for_ignores_manual_runs(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        # A manual run-now carries scheduled_for=now (large); it must not count
        # toward the scheduled-slot dedup or it would consume tonight's slot.
        await rn.create(db, scheduled_for=9_999_999.0, trigger="manual")
        assert await rn.last_scheduled_for(db) is None
        await rn.create(db, scheduled_for=1000.0, trigger="schedule")
        assert await rn.last_scheduled_for(db) == 1000.0
        await db.close()

    asyncio.run(go())


def test_observations_ordered_and_typed(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        nid = await rn.create(db, scheduled_for=0.0, trigger="manual")
        first = await rn.add_observation(
            db, nid, step="suite", severity="error", kind="suite.timeout", message="hung"
        )
        await rn.add_observation(
            db,
            nid,
            step="soak",
            severity="info",
            kind="soak.summary",
            message="done",
            data={"lines": 5},
        )
        obs = await rn.observations(db, nid)
        assert [o["kind"] for o in obs] == ["suite.timeout", "soak.summary"]
        assert obs[0]["id"] == first["id"] and obs[0]["data"] is None
        assert obs[1]["data"] == {"lines": 5}
        await db.close()

    asyncio.run(go())


def test_report_upsert_and_delivery_update(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        nid = await rn.create(db, scheduled_for=0.0)
        await rn.upsert_report(
            db,
            nid,
            status="gh_missing",
            issue_url=None,
            error="gh not found",
            title="Nightly",
            body_md="# body",
            failures=2,
            observation_count=3,
        )
        # Re-render overwrites in place (same PK).
        await rn.upsert_report(
            db,
            nid,
            status="posted",
            issue_url="https://github.com/x/y/issues/1",
            error=None,
            title="Nightly",
            body_md="# body2",
            failures=2,
            observation_count=3,
        )
        rep = await rn.get_report(db, nid)
        assert rep is not None and rep["status"] == "posted" and rep["body_md"] == "# body2"

        # Repost path touches only delivery fields.
        await rn.set_report_delivery(db, nid, status="network_error", issue_url=None, error="dns")
        rep = await rn.get_report(db, nid)
        assert rep is not None and rep["status"] == "network_error"
        assert rep["body_md"] == "# body2"
        await db.close()

    asyncio.run(go())


def test_results_for_run(tmp_path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        run_id = await rr.create_run(
            db, args=[], seed=None, fw_branch="develop", fw_sha="abc", fw_dirty=False
        )
        await rr.add_result(
            db,
            run_id,
            nodeid="tests/mesh/test_a.py::test_x",
            tier="mesh",
            outcome="failed",
            duration_s=1.0,
            device_serial="S1",
            longrepr="boom",
        )
        await rr.add_result(
            db,
            run_id,
            nodeid="tests/mesh/test_a.py::test_y",
            tier="mesh",
            outcome="passed",
            duration_s=2.0,
            device_serial=None,
            longrepr=None,
        )
        rows = await rr.results_for_run(db, run_id)
        assert [r["nodeid"].rsplit("::", 1)[1] for r in rows] == ["test_x", "test_y"]
        assert rows[0]["longrepr"] == "boom"
        await db.close()

    asyncio.run(go())
