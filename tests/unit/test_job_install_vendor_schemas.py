# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`job`, `install` and `vendor` results satisfy their output schemas on every return path."""

from __future__ import annotations

import asyncio
import time

import pytest

from meshtastic_mcp import confirm, hw_tools, jobs
from meshtastic_mcp.tools import hardware


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name: str, args: dict):
    from fastmcp import Client

    async def run():
        async with Client(server.app) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def _schema(server, name: str) -> dict:
    tools = {t.name: t for t in asyncio.run(server.app.list_tools())}
    return tools[name].output_schema


@pytest.mark.parametrize("name", ["job", "install", "vendor"])
def test_the_tool_advertises_named_fields(server, name) -> None:
    assert "properties" in str(_schema(server, name))


def test_job_list_and_one_finished_job_satisfy_the_schema(server) -> None:
    def body(state, log_path):
        with jobs.LOCK:
            state["status"] = "done"
            state["exit_code"] = 0
            state["finished_at"] = time.time()

    job_id = jobs.start("flashes", "schema-check", body)["job_id"]
    listed = _call(server, "job", {})
    assert listed.is_error is False, listed.content
    assert any(j["job_id"] == job_id for j in listed.structured_content["jobs"])
    one = _call(server, "job", {"job_id": job_id, "wait_s": 5})
    assert one.is_error is False, one.content
    assert one.structured_content["status"] == "done"


def test_a_vendor_read_satisfies_the_schema(server, monkeypatch) -> None:
    monkeypatch.setattr(
        hw_tools,
        "esptool_raw",
        lambda args, port=None, confirm=False: {
            "exit_code": 0,
            "stdout": "Chip is ESP32-S3",
            "stderr": "",
            "stdout_tail": "Chip is ESP32-S3",
            "stderr_tail": "",
            "duration_s": 0.4,
        },
    )
    result = _call(server, "vendor", {"tool": "esptool", "args": ["chip_id"]})
    assert result.is_error is False, result.content
    assert result.structured_content["exit_code"] == 0


def test_an_install_satisfies_the_schema(server, monkeypatch) -> None:
    async def agree(ctx, what, confirmed, irreversible=False):
        return None

    monkeypatch.setattr(confirm, "gate", agree)
    monkeypatch.setattr(
        hardware,
        "_install",
        lambda port, source, erase, userprefs, build_flags: {
            "method": "esptool-ota",
            "exit_code": 0,
            "stdout_tail": "Hash of data verified.",
            "stderr_tail": "",
            "back": True,
            "firmware_version": "2.8.4.abc123",
        },
    )
    result = _call(
        server,
        "install",
        {"port": "/dev/ttyUSB9", "source": {"env": "heltec-v3"}, "confirm": True},
    )
    assert result.is_error is False, result.content
    assert result.structured_content["firmware_version"] == "2.8.4.abc123"
    assert result.structured_content["job_id"]
