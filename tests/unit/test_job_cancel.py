# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Cancelling and awaiting jobs: builds stop, flashes refuse, and wait follows a job to its end."""

from __future__ import annotations

import asyncio
import time

import pytest

from meshtastic_mcp import jobs, pio


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))


def _sleeper(seconds: float):
    """A job body that runs a real subprocess through pio's capture path, as a build does."""

    def body(state, log_path):
        code, _out, _err, _dur = pio._run_capturing(["sleep", str(seconds)])
        with jobs.LOCK:
            state["exit_code"] = code
            state["finished_at"] = time.time()
            state["status"] = "done" if code == 0 else "failed"

    return body


def test_cancelling_a_cancellable_job_kills_its_process_and_reports_cancelled() -> None:
    started = jobs.start("builds", "x", _sleeper(30), cancellable=True)
    t0 = time.monotonic()
    time.sleep(0.3)
    assert jobs.cancel(started["job_id"])["cancelled"] is True
    final = asyncio.run(jobs.wait(started["job_id"]))
    assert final["status"] == "cancelled"
    assert time.monotonic() - t0 < 10


def test_a_non_cancellable_job_refuses_to_stop_and_finishes() -> None:
    started = jobs.start("flashes", "x", _sleeper(1))
    time.sleep(0.2)
    refusal = jobs.cancel(started["job_id"])
    assert refusal["cancelled"] is False and "cannot be interrupted" in refusal["reason"]
    final = asyncio.run(jobs.wait(started["job_id"]))
    assert final["status"] == "done"


def test_cancelling_the_waiter_cancels_a_cancellable_job() -> None:
    started = jobs.start("builds", "x", _sleeper(30), cancellable=True)

    async def run():
        task = asyncio.ensure_future(jobs.wait(started["job_id"], poll_s=0.05))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await jobs.wait(started["job_id"], poll_s=0.05)

    assert asyncio.run(run())["status"] == "cancelled"


def test_cancelling_the_waiter_leaves_a_flash_running_and_pollable() -> None:
    started = jobs.start("flashes", "x", _sleeper(1.5))

    async def run():
        task = asyncio.ensure_future(jobs.wait(started["job_id"], poll_s=0.05))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert jobs.poll(started["job_id"])["status"] == "running"
        return await jobs.wait(started["job_id"], poll_s=0.05)

    assert asyncio.run(run())["status"] == "done"


def test_unknown_job_ids_are_reported() -> None:
    assert "error" in jobs.cancel("nope")
    assert "error" in asyncio.run(jobs.wait("nope"))
