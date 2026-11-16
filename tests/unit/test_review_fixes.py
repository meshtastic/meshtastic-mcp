# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Gated tools stay off the event loop, ask inside background tasks, and bind each approval."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ElicitResult, InputRequiredResult

from meshtastic_mcp import admin, confirm, flash, hw_tools, jobs, pio


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(confirm.POLICY_ENV, raising=False)


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _answer():
    seen: list[str] = []

    async def handler(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept", content={"confirm": True, "value": True})

    return handler, seen


# ---- blocking device work runs off the event loop -------------------------------


def test_a_slow_gated_tool_does_not_stall_other_calls(server, monkeypatch) -> None:
    monkeypatch.setattr(
        admin,
        "reboot",
        lambda port=None, confirm=False, seconds=10: time.sleep(1.5) or {"ok": True},
    )
    app = FastMCP("t")
    app.tool()(getattr(server.reboot, "fn", server.reboot))

    @app.tool()
    def ping() -> str:
        return "pong"

    done: list[str] = []

    async def call(client, name, args):
        await client.call_tool(name, args)
        done.append(name)

    async def run():
        # Client and server share this loop in memory, so a tool that blocks it also delays
        # the ping's start; the order the two finish in is what tells them apart.
        async with Client(app) as client:
            slow = asyncio.ensure_future(call(client, "reboot", {"confirm": True}))
            fast = asyncio.ensure_future(call(client, "ping", {}))
            await asyncio.gather(slow, fast)

    asyncio.run(run())
    assert done == ["ping", "reboot"], "the reboot held the event loop"


# ---- the gate asks inside a background task -------------------------------------


@pytest.mark.parametrize(
    ("args", "policy"),
    [
        ({"env": "e", "port": "/dev/x"}, "agent"),
        ({"env": "e", "port": "/dev/x", "confirm": True}, "human"),
    ],
)
def test_a_task_asks_the_human_and_then_flashes(server, monkeypatch, args, policy) -> None:
    tasks_mod = pytest.importorskip("fastmcp_tasks")
    monkeypatch.setenv(confirm.POLICY_ENV, policy)
    calls: list[bool] = []
    monkeypatch.setattr(
        flash, "erase_and_flash", lambda *a, **k: calls.append(k["confirm"]) or {"exit_code": 0}
    )
    app = FastMCP("t")
    app.add_extension(tasks_mod.TasksExtension())
    app.tool(task=True)(getattr(server.erase_and_flash, "fn", server.erase_and_flash))
    handler, seen = _answer()

    async def run():
        async with Client(app, elicitation_handler=handler) as client:
            task = await tasks_mod.call_tool_task(client, "erase_and_flash", args)
            return await task.result()

    result = asyncio.run(run())
    assert result.is_error is False
    assert len(seen) == 1 and calls == [True]


# ---- an approval is bound to the action it was asked for -------------------------


def _ctx(input_responses=None, request_state=None):
    return SimpleNamespace(
        is_background_task=False,
        session=SimpleNamespace(client_capabilities=SimpleNamespace(elicitation=object())),
        request_context=SimpleNamespace(protocol_version="2026-07-28"),
        input_responses=input_responses,
        request_state=request_state,
    )


def _accepted():
    return {"confirm": SimpleNamespace(action="accept", content={"confirm": True})}


def test_an_unasked_answer_is_asked_again() -> None:
    out = asyncio.run(confirm.gate(_ctx(_accepted(), None), "reboot /dev/x", False))
    assert isinstance(out, InputRequiredResult)


def test_an_answer_for_other_arguments_is_asked_again() -> None:
    stale = confirm._binding("reboot /dev/x")
    out = asyncio.run(confirm.gate(_ctx(_accepted(), stale), "reboot /dev/y", False))
    assert isinstance(out, InputRequiredResult)


def test_the_answer_to_this_question_proceeds() -> None:
    state = confirm._binding("reboot /dev/x")
    assert asyncio.run(confirm.gate(_ctx(_accepted(), state), "reboot /dev/x", False)) is None


def test_the_question_carries_its_binding() -> None:
    out = asyncio.run(confirm.gate(_ctx(), "reboot /dev/x", False))
    assert isinstance(out, InputRequiredResult)
    assert out.request_state == confirm._binding("reboot /dev/x")


# ---- jobs ------------------------------------------------------------------------


def test_a_failed_run_sync_job_polls_as_failed_with_its_output() -> None:
    result = asyncio.run(
        jobs.run_sync("flashes", "x", lambda: {"exit_code": 2, "stderr_tail": "no chip"})
    )
    polled = jobs.poll(result["job_id"])
    assert result["exit_code"] == 2
    assert polled["status"] == "failed" and polled["exit_code"] == 2
    assert "no chip" in "\n".join(polled["log_tail"])


def test_a_wrong_port_run_sync_job_polls_as_failed() -> None:
    result = asyncio.run(
        jobs.run_sync("flashes", "x", lambda: {"exit_code": 0, "upload_port_mismatch": "y"})
    )
    assert jobs.poll(result["job_id"])["status"] == "failed"


def test_a_waiter_that_does_not_own_the_job_leaves_it_running() -> None:
    def body(state, log_path):
        code, *_ = pio._run_capturing(["sleep", "1"])
        with jobs.LOCK:
            state["exit_code"] = code
            state["status"] = "done"

    started = jobs.start("builds", "x", body, cancellable=True)

    async def run():
        task = asyncio.ensure_future(jobs.wait(started["job_id"], poll_s=0.05, cancel_job=False))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert jobs.poll(started["job_id"])["status"] == "running"
        return await jobs.wait(started["job_id"], poll_s=0.05)

    assert asyncio.run(run())["status"] == "done"


def test_cancel_does_not_signal_a_process_that_already_exited(monkeypatch) -> None:
    signalled: list[int] = []
    monkeypatch.setattr(jobs.os, "killpg", lambda pid, sig: signalled.append(pid))
    finished = SimpleNamespace(pid=999999, poll=lambda: 0)
    with jobs.LOCK:
        jobs._active["deadproc"] = {
            "job_id": "deadproc",
            "kind": "builds",
            "status": "running",
            "cancellable": True,
            "proc": finished,
        }
    try:
        assert jobs.cancel("deadproc")["cancelled"] is True
        assert signalled == []
    finally:
        with jobs.LOCK:
            jobs._active.pop("deadproc", None)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_timed_out_cancellable_run_kills_the_children_too() -> None:
    marker = "31.734"

    def body(state, log_path):
        try:
            pio._run_capturing(["sh", "-c", f"sleep {marker} & sleep {marker}"], timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        with jobs.LOCK:
            state["status"] = "done"

    started = jobs.start("builds", "x", body, cancellable=True)
    asyncio.run(jobs.wait(started["job_id"], poll_s=0.05))
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", f"children survived: {left.stdout}"


# ---- hw subcommand classification ------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--port", "/dev/x", "erase_flash"],
        ["-b", "921600", "write_flash", "0x0", "fw.bin"],
        ["--chip", "esp32s3", "erase-region", "0x0", "0x1000"],
    ],
)
def test_a_flag_value_cannot_hide_a_destructive_subcommand(monkeypatch, args) -> None:
    monkeypatch.setattr(hw_tools.config, "esptool_bin", lambda: "esptool")
    monkeypatch.setattr(hw_tools, "_run", lambda *a, **k: pytest.fail("ran without confirm"))
    with pytest.raises(hw_tools.ConfirmRequired):
        hw_tools.esptool_raw(args)


def test_a_harmless_subcommand_with_flags_still_runs(monkeypatch) -> None:
    monkeypatch.setattr(hw_tools.config, "esptool_bin", lambda: "esptool")
    monkeypatch.setattr(hw_tools, "_run", lambda b, args, **k: {"exit_code": 0, "args": args})
    assert hw_tools.esptool_raw(["--port", "/dev/x", "flash_id"])["exit_code"] == 0
