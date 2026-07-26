# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly-bake history: one ``nightly_runs`` row per night, a stream of
``nightly_observations`` (infra events + analysis findings) per run, and one
``nightly_reports`` row holding the rendered report + delivery status."""

from __future__ import annotations

import json
import time

from .database import Database

_RUN_COLS = (
    "id, scheduled_for, started_at, finished_at, status, step, trigger, run_id, "
    "suite_attempts, soak_started_at, mcp_sha_before, mcp_sha_after, "
    "fw_sha_before, fw_sha_after, summary"
)

UNFINISHED = ("running", "awaiting_restart")


def _run_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
    except (ValueError, TypeError):
        d["summary"] = None
    return d


def _obs_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["data"] = json.loads(d["data"]) if d.get("data") else None
    except (ValueError, TypeError):
        d["data"] = None
    return d


# --- nightly_runs -----------------------------------------------------------


async def create(db: Database, *, scheduled_for: float, trigger: str = "schedule") -> int:
    cur = await db.execute(
        "INSERT INTO nightly_runs (scheduled_for, started_at, trigger) VALUES (?,?,?)",
        (scheduled_for, time.time(), trigger),
    )
    assert cur.lastrowid is not None  # set after any successful INSERT
    return cur.lastrowid


async def get(db: Database, nightly_id: int) -> dict | None:
    row = await db.fetchone(f"SELECT {_RUN_COLS} FROM nightly_runs WHERE id=?", (nightly_id,))
    return _run_to_dict(row) if row is not None else None


async def latest_unfinished(db: Database) -> dict | None:
    row = await db.fetchone(
        f"SELECT {_RUN_COLS} FROM nightly_runs WHERE status IN (?,?) ORDER BY id DESC LIMIT 1",
        UNFINISHED,
    )
    return _run_to_dict(row) if row is not None else None


async def last_scheduled_for(db: Database) -> float | None:
    """The most recent SCHEDULED slot ever attempted — drives catch-up. Manual
    (run-now) rows carry an arbitrary ``scheduled_for=now`` and must not count,
    or a manual run just after the nightly time would silently consume that
    night's scheduled slot (or a manual run just before would double-bake it)."""
    row = await db.fetchone(
        "SELECT MAX(scheduled_for) AS m FROM nightly_runs WHERE trigger='schedule'", ()
    )
    return float(row["m"]) if row is not None and row["m"] is not None else None


async def list_runs(db: Database, limit: int = 30) -> list[dict]:
    rows = await db.fetchall(
        f"SELECT {_RUN_COLS} FROM nightly_runs ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [_run_to_dict(r) for r in rows]


async def set_step(db: Database, nightly_id: int, step: str) -> None:
    await db.execute("UPDATE nightly_runs SET step=? WHERE id=?", (step, nightly_id))


async def set_status(db: Database, nightly_id: int, status: str) -> None:
    await db.execute("UPDATE nightly_runs SET status=? WHERE id=?", (status, nightly_id))


async def set_run_id(db: Database, nightly_id: int, run_id: int) -> None:
    await db.execute("UPDATE nightly_runs SET run_id=? WHERE id=?", (run_id, nightly_id))


async def bump_suite_attempts(db: Database, nightly_id: int) -> int:
    await db.execute(
        "UPDATE nightly_runs SET suite_attempts=suite_attempts+1 WHERE id=?", (nightly_id,)
    )
    row = await db.fetchone("SELECT suite_attempts FROM nightly_runs WHERE id=?", (nightly_id,))
    return int(row["suite_attempts"]) if row is not None else 0


async def set_soak_started(db: Database, nightly_id: int, ts: float) -> None:
    await db.execute("UPDATE nightly_runs SET soak_started_at=? WHERE id=?", (ts, nightly_id))


async def set_shas(
    db: Database,
    nightly_id: int,
    *,
    mcp_before: str | None = None,
    mcp_after: str | None = None,
    fw_before: str | None = None,
    fw_after: str | None = None,
) -> None:
    """Update whichever sha columns are provided (None = leave unchanged)."""
    sets: list[str] = []
    params: list[object] = []
    for col, val in (
        ("mcp_sha_before", mcp_before),
        ("mcp_sha_after", mcp_after),
        ("fw_sha_before", fw_before),
        ("fw_sha_after", fw_after),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    params.append(nightly_id)
    await db.execute(f"UPDATE nightly_runs SET {', '.join(sets)} WHERE id=?", tuple(params))


async def finish(db: Database, nightly_id: int, *, status: str, summary: dict | None) -> None:
    await db.execute(
        "UPDATE nightly_runs SET finished_at=?, status=?, summary=? WHERE id=?",
        (time.time(), status, json.dumps(summary) if summary is not None else None, nightly_id),
    )


# --- nightly_observations ---------------------------------------------------


async def add_observation(
    db: Database,
    nightly_id: int,
    *,
    step: str,
    severity: str,
    kind: str,
    message: str,
    data: dict | None = None,
) -> dict:
    ts = time.time()
    cur = await db.execute(
        "INSERT INTO nightly_observations (nightly_id, step, severity, kind, message, data, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (nightly_id, step, severity, kind, message, json.dumps(data) if data else None, ts),
    )
    return {
        "id": cur.lastrowid,
        "nightly_id": nightly_id,
        "step": step,
        "severity": severity,
        "kind": kind,
        "message": message,
        "data": data,
        "ts": ts,
    }


async def observations(db: Database, nightly_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT id, nightly_id, step, severity, kind, message, data, ts "
        "FROM nightly_observations WHERE nightly_id=? ORDER BY id",
        (nightly_id,),
    )
    return [_obs_to_dict(r) for r in rows]


# --- nightly_reports --------------------------------------------------------


async def upsert_report(
    db: Database,
    nightly_id: int,
    *,
    status: str,
    issue_url: str | None,
    error: str | None,
    title: str | None,
    body_md: str | None,
    failures: int,
    observation_count: int,
) -> None:
    await db.execute(
        "INSERT INTO nightly_reports "
        "(nightly_run_id, created_at, status, issue_url, error, title, body_md, "
        " failures, observations) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(nightly_run_id) DO UPDATE SET "
        "status=excluded.status, issue_url=excluded.issue_url, error=excluded.error, "
        "title=excluded.title, body_md=excluded.body_md, failures=excluded.failures, "
        "observations=excluded.observations",
        (
            nightly_id,
            time.time(),
            status,
            issue_url,
            error,
            title,
            body_md,
            failures,
            observation_count,
        ),
    )


async def set_report_delivery(
    db: Database, nightly_id: int, *, status: str, issue_url: str | None, error: str | None
) -> None:
    """Update only the delivery fields (used by repost — body stays intact)."""
    await db.execute(
        "UPDATE nightly_reports SET status=?, issue_url=?, error=? WHERE nightly_run_id=?",
        (status, issue_url, error, nightly_id),
    )


async def get_report(db: Database, nightly_id: int) -> dict | None:
    row = await db.fetchone(
        "SELECT nightly_run_id, created_at, status, issue_url, error, title, body_md, "
        "failures, observations FROM nightly_reports WHERE nightly_run_id=?",
        (nightly_id,),
    )
    return dict(row) if row is not None else None
