# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Every destructive tool asks the human when it can, and flashes run as awaited jobs."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ElicitResult

from meshtastic_mcp import confirm, flash, hw_tools, jobs
from tests.unit._tool_fns import tool_fn


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(confirm.POLICY_ENV, "agent")


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _answer(action: str = "accept"):
    seen: list[str] = []

    async def handler(message, response_type, params, context):
        seen.append(message)
        content = {"confirm": True, "value": True} if action == "accept" else None
        return ElicitResult(action=action, content=content)

    return handler, seen


def _app_with(server, *names: str) -> FastMCP:
    app = FastMCP("t")
    for name in names:
        tool = tool_fn(name)
        app.tool()(getattr(tool, "fn", tool))
    return app


def _call(app, name, args, handler=None, mode=None):
    kwargs = {"mode": mode} if mode else {}

    async def run():
        async with Client(app, elicitation_handler=handler, **kwargs) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


# ---- jobs.run_sync -----------------------------------------------------------


def test_run_sync_returns_the_result_with_its_job_id() -> None:
    out = asyncio.run(jobs.run_sync("flashes", "x", lambda: {"ok": True}))
    assert out["ok"] is True and jobs.poll(out["job_id"])["status"] == "done"


def test_run_sync_raises_when_the_function_fails() -> None:
    def boom():
        raise ValueError("no board")

    with pytest.raises(RuntimeError, match="no board"):
        asyncio.run(jobs.run_sync("flashes", "x", boom))


def test_cancelling_a_run_sync_waiter_leaves_the_flash_running() -> None:
    def slow():
        time.sleep(1.0)
        return {"ok": True}

    async def run():
        task = asyncio.ensure_future(jobs.run_sync("flashes", "x", slow))
        await asyncio.sleep(0.3)
        job_id = jobs.running_of_kind("flashes")[0]["job_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert jobs.poll(job_id)["status"] == "running"
        return await jobs.wait(job_id, poll_s=0.05)

    assert asyncio.run(run())["status"] == "done"


# ---- hw tools: ask only when the subcommand is destructive ---------------------


@pytest.fixture
def hw_runs(monkeypatch, server):
    calls: list[list[str]] = []
    monkeypatch.setattr(hw_tools.config, "esptool_bin", lambda: "esptool")
    monkeypatch.setattr(
        hw_tools,
        "_run",
        lambda binary, args, **_kw: calls.append(list(args)) or {"exit_code": 0},
    )
    return calls


def test_a_harmless_esptool_subcommand_never_asks(hw_runs, server) -> None:
    handler, seen = _answer()
    app = _app_with(server, "esptool_raw")
    assert _call(app, "esptool_raw", {"args": ["flash_id"]}, handler).is_error is False
    assert seen == [] and hw_runs == [["flash_id"]]


def test_a_destructive_esptool_subcommand_asks_then_runs(hw_runs, server) -> None:
    handler, seen = _answer()
    app = _app_with(server, "esptool_raw")
    assert _call(app, "esptool_raw", {"args": ["erase_flash"]}, handler).is_error is False
    assert len(seen) == 1 and "esptool erase_flash" in seen[0] and hw_runs == [["erase_flash"]]


def test_declining_a_destructive_esptool_call_runs_nothing(hw_runs, server) -> None:
    handler, _ = _answer("decline")
    app = _app_with(server, "esptool_raw")
    assert _call(app, "esptool_raw", {"args": ["erase_flash"]}, handler).is_error is True
    assert hw_runs == []


def test_a_client_that_cannot_ask_still_gets_the_confirm_refusal(hw_runs, server) -> None:
    app = _app_with(server, "esptool_raw")
    result = _call(app, "esptool_raw", {"args": ["erase_flash"]})
    assert result.is_error is True and "confirm=True" in result.content[0].text
    assert hw_runs == []


def test_confirm_true_runs_without_asking_in_agent_policy(hw_runs, server) -> None:
    handler, seen = _answer()
    app = _app_with(server, "esptool_raw")
    args = {"args": ["erase_flash"], "confirm": True}
    assert _call(app, "esptool_raw", args, handler).is_error is False
    assert seen == [] and hw_runs == [["erase_flash"]]


@pytest.mark.parametrize("mode", [None, "legacy"])
def test_human_policy_asks_even_with_confirm_true(hw_runs, server, monkeypatch, mode) -> None:
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, seen = _answer()
    app = _app_with(server, "esptool_raw")
    args = {"args": ["erase_flash"], "confirm": True}
    assert _call(app, "esptool_raw", args, handler, mode).is_error is False
    assert len(seen) == 1 and hw_runs == [["erase_flash"]]


def test_human_policy_refuses_confirm_true_from_a_client_that_cannot_ask(
    hw_runs, server, monkeypatch
) -> None:
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    app = _app_with(server, "esptool_raw")
    result = _call(app, "esptool_raw", {"args": ["erase_flash"], "confirm": True})
    assert result.is_error is True and "cannot ask" in result.content[0].text
    assert hw_runs == []


def test_human_policy_does_not_ask_for_a_harmless_subcommand(hw_runs, server, monkeypatch) -> None:
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, seen = _answer()
    app = _app_with(server, "esptool_raw")
    assert _call(app, "esptool_raw", {"args": ["flash_id"]}, handler).is_error is False
    assert seen == []


# ---- flash tools: ask, then run as an awaited job ------------------------------


@pytest.fixture
def flashed(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake(name):
        def _fn(env, port, confirm=False, **_kw):
            calls.append((name, confirm))
            return {"exit_code": 0, "env": env, "port": port}

        return _fn

    monkeypatch.setattr(flash, "flash", fake("flash"))
    monkeypatch.setattr(flash, "erase_and_flash", fake("erase_and_flash"))
    monkeypatch.setattr(flash, "update_flash", fake("update_flash"))
    return calls


@pytest.mark.parametrize("tool", ["pio_flash", "erase_and_flash", "update_flash"])
def test_flash_tools_ask_then_run_as_a_job(flashed, server, tool) -> None:
    handler, seen = _answer()
    app = _app_with(server, tool)
    result = _call(app, tool, {"env": "e", "port": "/dev/x"}, handler)
    assert result.is_error is False
    data = result.structured_content
    assert data["exit_code"] == 0 and data["job_id"]
    assert len(seen) == 1 and flashed == [(tool.replace("pio_", ""), True)]


@pytest.mark.parametrize("tool", ["pio_flash", "erase_and_flash", "update_flash"])
def test_declining_a_flash_runs_nothing(flashed, server, tool) -> None:
    handler, _ = _answer("decline")
    app = _app_with(server, tool)
    assert _call(app, tool, {"env": "e", "port": "/dev/x"}, handler).is_error is True
    assert flashed == []


def test_erase_and_flash_asks_under_human_policy_but_update_flash_does_not(
    flashed, server, monkeypatch
) -> None:
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, seen = _answer()
    app = _app_with(server, "erase_and_flash", "update_flash")
    args = {"env": "e", "port": "/dev/x", "confirm": True}
    assert _call(app, "update_flash", args, handler).is_error is False
    assert seen == []
    assert _call(app, "erase_and_flash", args, handler).is_error is False
    assert len(seen) == 1


def test_a_failing_flash_is_a_tool_error(monkeypatch, server) -> None:
    def boom(*_a, **_kw):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(flash, "flash", boom)
    app = _app_with(server, "pio_flash")
    result = _call(app, "pio_flash", {"env": "e", "port": "/dev/x", "confirm": True})
    assert result.is_error is True and "upload failed" in result.content[0].text


# ---- uhubctl: only powering off asks -------------------------------------------


def test_uhubctl_power_on_never_asks_but_off_does(monkeypatch, server) -> None:
    from meshtastic_mcp import uhubctl

    seen_calls: list[str] = []
    monkeypatch.setattr(uhubctl, "power_on", lambda loc, p: seen_calls.append("on") or {"ok": 1})
    monkeypatch.setattr(uhubctl, "power_off", lambda loc, p: seen_calls.append("off") or {"ok": 1})
    handler, seen = _answer()
    app = _app_with(server, "uhubctl_power")
    on = {"action": "on", "location": "1-1", "port": 2}
    off = {"action": "off", "location": "1-1", "port": 2}
    assert _call(app, "uhubctl_power", on, handler).is_error is False
    assert seen == []
    assert _call(app, "uhubctl_power", off, handler).is_error is False
    assert len(seen) == 1 and seen_calls == ["on", "off"]
