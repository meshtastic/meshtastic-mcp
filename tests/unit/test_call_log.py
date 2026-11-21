# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from meshtastic_mcp import call_log
from meshtastic_mcp.call_log import CallLogMiddleware


def _app() -> FastMCP:
    app = FastMCP("t")
    app.add_middleware(CallLogMiddleware())

    @app.tool()
    def flash(port: str, private_key: str) -> dict:
        raise ToolError("no UF2 drive for that board; mount it and retry")

    @app.tool()
    def boom(node: str) -> dict:
        raise RuntimeError("library blew up")

    @app.tool()
    def start(node: str) -> dict:
        return {"job_id": "job-7"}

    @app.tool()
    def build_poll(build_id: str) -> dict:
        return {"status": "running"}

    return app


def _call(app: FastMCP, name: str, args: dict) -> None:
    async def go() -> None:
        async with Client(app) as client:
            await client.call_tool(name, args, raise_on_error=False)

    asyncio.run(go())


@pytest.fixture
def caplog_calls(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    call_log.log.addHandler(caplog.handler)
    level = call_log.log.level
    call_log.log.setLevel(logging.DEBUG)
    yield caplog
    call_log.log.setLevel(level)
    call_log.log.removeHandler(caplog.handler)


def test_a_tool_error_logs_its_reason_target_and_arg_names_but_no_values(caplog_calls) -> None:
    _call(_app(), "flash", {"port": "/dev/ttyACM0", "private_key": "SECRETKEYVALUE"})
    text = caplog_calls.text
    assert "tool=flash" in text and "target=/dev/ttyACM0" in text
    assert "no UF2 drive for that board" in text
    assert "args=[port,private_key]" in text
    assert "SECRETKEYVALUE" not in text
    assert any(r.levelno == logging.WARNING for r in caplog_calls.records)


def test_an_unexpected_exception_logs_its_type_and_traceback(caplog_calls) -> None:
    _call(_app(), "boom", {"node": "heltec"})
    rec = next(r for r in caplog_calls.records if "tool=boom" in r.getMessage())
    assert "RuntimeError" in rec.getMessage() and "library blew up" in rec.getMessage()
    assert rec.exc_info is not None


def test_success_logs_one_info_line_with_the_job_id(caplog_calls) -> None:
    _call(_app(), "start", {"node": "rak"})
    rec = next(r for r in caplog_calls.records if "tool=start" in r.getMessage())
    assert rec.levelno == logging.INFO
    assert "target=rak" in rec.getMessage() and "job=job-7" in rec.getMessage()


def test_polled_reads_log_at_debug(caplog_calls) -> None:
    _call(_app(), "build_poll", {"build_id": "b"})
    rec = next(r for r in caplog_calls.records if "tool=build_poll" in r.getMessage())
    assert rec.levelno == logging.DEBUG


def test_lines_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _call(_app(), "flash", {"port": "p", "private_key": "k"})
    out, err = capsys.readouterr()
    assert "tool=flash" not in out
    assert "tool=flash" in err


def test_the_server_registers_the_middleware() -> None:
    from meshtastic_mcp import server

    assert any(isinstance(m, CallLogMiddleware) for m in server.app.middleware)
