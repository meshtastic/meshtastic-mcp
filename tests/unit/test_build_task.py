# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`build` runs as a background task, and cancelling the task stops the build."""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastmcp_tasks")

from fastmcp import Client, FastMCP
from fastmcp_tasks import TasksExtension, call_tool_task

from meshtastic_mcp import flash, jobs, pio


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))


@pytest.fixture
def build_app(monkeypatch):
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server

    def fake_build_start(env, **_kw):
        def body(state, log_path):
            code, _o, _e, _d = pio._run_capturing(["sleep", str(SLEEP_S)])
            log_path.write_text("built\n", encoding="utf-8")
            with jobs.LOCK:
                state["exit_code"] = code
                state["finished_at"] = time.time()
                state["duration_s"] = 0.0
                state["artifacts"] = ["firmware.bin"]
                state["status"] = "done" if code == 0 else "failed"

        return jobs.start("builds", env, body, cancellable=True)

    monkeypatch.setattr(flash, "build_start", fake_build_start)
    app = FastMCP("t")
    app.add_extension(TasksExtension())
    app.tool(task=True)(getattr(server.build, "fn", server.build))
    return app


SLEEP_S = 0.5


def test_build_returns_the_finished_job_as_a_task(build_app) -> None:
    async def run():
        async with Client(build_app) as client:
            task = await call_tool_task(client, "build", {"env": "e"})
            return await task.result()

    result = asyncio.run(run())
    assert result.is_error is False
    data = result.structured_content
    assert data["status"] == "done" and data["artifacts"] == ["firmware.bin"]
    assert data["job_id"]


def test_cancelling_the_task_cancels_the_build_job(build_app) -> None:
    global SLEEP_S
    SLEEP_S = 30

    async def run():
        async with Client(build_app) as client:
            task = await call_tool_task(client, "build", {"env": "e"})
            await asyncio.sleep(0.6)
            await task.cancel()
            for _ in range(60):
                running = jobs.running_of_kind("builds")
                if not running:
                    break
                await asyncio.sleep(0.1)
            return running

    try:
        t0 = time.monotonic()
        assert asyncio.run(run()) == []
        assert time.monotonic() - t0 < 15
    finally:
        SLEEP_S = 0.5
