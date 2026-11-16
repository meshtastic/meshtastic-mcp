# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Without the `[tasks]` extra nothing changes: tools are not task-marked and `build` runs inline."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp import Client, FastMCP

from meshtastic_mcp import flash, jobs, pio, tasks


def test_task_kwargs_are_empty_without_the_extension(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_extension", None)
    assert tasks.available() is False
    assert tasks.task_kwargs() == {}
    app = FastMCP("t")
    tasks.install(app)  # a no-op, must not raise


def test_task_kwargs_mark_tools_when_the_extension_is_present() -> None:
    pytest.importorskip("fastmcp_tasks")
    assert tasks.task_kwargs() == {"task": True}


def test_build_runs_inline_on_a_plain_app(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    from meshtastic_mcp import server

    def fake_build_start(env, **_kw):
        def body(state, log_path):
            code, _o, _e, _d = pio._run_capturing(["true"])
            log_path.write_text("built\n", encoding="utf-8")
            with jobs.LOCK:
                state["exit_code"] = code
                state["finished_at"] = time.time()
                state["duration_s"] = 0.0
                state["artifacts"] = ["firmware.bin"]
                state["status"] = "done"

        return jobs.start("builds", env, body, cancellable=True)

    monkeypatch.setattr(flash, "build_start", fake_build_start)
    app = FastMCP("t")
    app.tool()(getattr(server.build, "fn", server.build))

    async def run():
        async with Client(app) as client:
            return await client.call_tool("build", {"env": "e"})

    data = asyncio.run(run()).structured_content
    assert data["status"] == "done" and data["exit_code"] == 0 and data["log_tail"] == ["built"]
