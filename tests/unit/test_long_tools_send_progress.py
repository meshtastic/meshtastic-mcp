# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Build and flash tools that wait minutes send MCP progress notifications while they wait.

No real pio, esptool or nrfutil runs here: `pio.subprocess` and `flash.subprocess` are replaced
by a fake that starts a scripted Python child in place of whatever argv the code builds, so the
port string below never reaches a flashing tool.
"""

from __future__ import annotations

import asyncio
import itertools
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from meshtastic_mcp import config, confirm, flash, jobs, pio, port_recovery, userprefs
from tests.unit._tool_fns import tool_fn

PORT = "/dev/progress-test-port"

# Prints each argument as a line; `@sleep <s>` pauses instead, so a state outlasts a poll.
_SCRIPT = (
    "import sys, time\n"
    "for arg in sys.argv[1:]:\n"
    "    if arg.startswith('@sleep '):\n"
    "        time.sleep(float(arg.split()[1]))\n"
    "    else:\n"
    "        print(arg, flush=True)\n"
)

# A poll of the job lands at least once inside each pause.
_HOLD = "@sleep 0.7"

ESPTOOL_V5_UPLOAD = [
    "Processing heltec-v3 (board: heltec_wifi_lora_32_V3; platform: espressif32)",
    "Linking .pio/build/heltec-v3/firmware.elf",
    _HOLD,
    f"Serial port {PORT}:",
    "Flash will be erased from 0x00010000 to 0x0001ffff...",
    "Writing at 0x00014000 [=======                       ]  25.0% 16.0kB/64.0kB [00:01]",
    _HOLD,
    "Writing at 0x00018000 [===============               ]  50.0% 32.0kB/64.0kB [00:02]",
    _HOLD,
    "Writing at 0x0001c000 [======================        ]  75.0% 48.0kB/64.0kB [00:03]",
    _HOLD,
    "Hash of data verified.",
    "Hard resetting via RTS pin...",
]

COMPILE = [
    "Compiling .pio/build/tbeam/src/main.cpp.o",
    _HOLD,
    "Compiling .pio/build/tbeam/src/mesh/Router.cpp.o",
    _HOLD,
    "Linking .pio/build/tbeam/firmware.elf",
    _HOLD,
    "RAM:   [==        ]  15.2% (used 49820 bytes from 327680 bytes)",
    _HOLD,
    "========================= [SUCCESS] Took 12.34 seconds =========================",
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(confirm.POLICY_ENV, "agent")
    monkeypatch.delenv("MESHTASTIC_MCP_FLASH_LOG", raising=False)


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


@pytest.fixture
def scripted(monkeypatch):
    """Replace every subprocess pio and flash start with a child printing the given lines."""
    asked: list[list[str]] = []
    lines: list[str] = []

    def child(argv):
        asked.append([str(a) for a in argv])
        return [sys.executable, "-c", _SCRIPT, *lines]

    fake = SimpleNamespace(
        PIPE=subprocess.PIPE,
        TimeoutExpired=subprocess.TimeoutExpired,
        Popen=lambda argv, **kw: subprocess.Popen(child(argv), **kw),
        run=lambda argv, **kw: subprocess.run(child(argv), **kw),
    )
    monkeypatch.setattr(pio, "subprocess", fake)
    monkeypatch.setattr(flash, "subprocess", fake)

    def script(*output: str) -> list[list[str]]:
        lines[:] = output
        return asked

    return script


@pytest.fixture
def firmware_tree(tmp_path, monkeypatch):
    @contextmanager
    def no_overrides(_overrides=None):
        yield {}

    monkeypatch.setattr(config, "pio_bin", lambda: Path("/nonexistent/pio"))
    monkeypatch.setattr(config, "firmware_root", lambda: tmp_path)
    monkeypatch.setattr(userprefs, "temporary_overrides", no_overrides)
    # The pre-flight would open the port or power-cycle its hub slot; the port is never real here.
    monkeypatch.setattr(port_recovery, "ensure_port_free", lambda port, **_kw: port)


def _app(server, *names: str, task: bool = False) -> FastMCP:
    app = FastMCP("t")
    for name in names:
        tool = tool_fn(name)
        register = app.tool(task=True) if task else app.tool()
        register(getattr(tool, "fn", tool))
    return app


def _call_with_progress(app, name, args):
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def run():
        async with Client(app) as client:
            result = await client.call_tool(
                name, args, progress_handler=on_progress, raise_on_error=False
            )
            await asyncio.sleep(0.1)  # handlers run as their own tasks
            return result

    return asyncio.run(run()), seen


def _strictly_rising(seen) -> bool:
    values = [p for p, _t, _m in seen]
    return all(b > a for a, b in itertools.pairwise(values))


def test_pio_flash_reports_the_esptool_write_as_bytes_of_the_image(
    server, scripted, firmware_tree, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    asked = scripted(*ESPTOOL_V5_UPLOAD)

    result, seen = _call_with_progress(
        _app(server, "pio_flash"), "pio_flash", {"env": "heltec-v3", "port": PORT, "confirm": True}
    )

    assert result.is_error is False
    assert result.structured_content["exit_code"] == 0 and result.structured_content["job_id"]
    assert asked and asked[0][0] == "/nonexistent/pio" and PORT in asked[0]
    writes = [(p, t, m) for p, t, m in seen if m and m.startswith("Writing at")]
    assert len(writes) >= 3, seen
    # Message-only updates before the write add a sliver to progress and total alike.
    assert all(t == pytest.approx(0x10000, abs=1) for _p, t, _m in writes)
    assert [round(p / t, 2) for p, t, _m in writes[:3]] == [0.25, 0.5, 0.75]
    assert writes[0][2] == "Writing at 0x00014000 (25.0%)"
    assert _strictly_rising(seen)


def test_update_flash_reports_the_install_scripts_esptool_write(
    server, scripted, firmware_tree, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    monkeypatch.setattr(config, "esptool_bin", lambda: Path("/nonexistent/esptool"))
    monkeypatch.setattr(flash, "_check_esp32_env", lambda env: "esp32s3")
    monkeypatch.setattr(flash, "_firmware_bin_for", lambda env: tmp_path / "firmware-tbeam.bin")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "device-update.sh").write_text("# never run\n", encoding="utf-8")
    asked = scripted(*ESPTOOL_V5_UPLOAD[3:])

    result, seen = _call_with_progress(
        _app(server, "update_flash"),
        "update_flash",
        {"env": "tbeam", "port": PORT, "confirm": True, "skip_build": True},
    )

    assert result.is_error is False, result.content
    assert asked[0][0].endswith("device-update.sh") and PORT in asked[0]
    writes = [(p, t) for p, t, m in seen if m and m.startswith("Writing at")]
    assert [round(p / t, 2) for p, t in writes[:3]] == [0.25, 0.5, 0.75], seen
    assert _strictly_rising(seen)


def test_build_reports_compile_lines_as_messages_without_a_total(
    server, scripted, firmware_tree, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    scripted(*COMPILE)

    result, seen = _call_with_progress(_app(server, "build"), "build", {"env": "tbeam"})

    assert result.is_error is False and result.structured_content["status"] == "done"
    messages = [m for _p, _t, m in seen]
    assert "Compiling .pio/build/tbeam/src/main.cpp.o" in messages
    assert "Linking .pio/build/tbeam/firmware.elf" in messages
    # PlatformIO's RAM/Flash usage bars print a percent that is not progress.
    assert all(t is None for _p, t, _m in seen), seen
    assert _strictly_rising(seen)


def test_a_chatty_build_is_throttled(server, scripted, firmware_tree) -> None:
    scripted(*[f"Compiling .pio/build/tbeam/src/file{i}.cpp.o" for i in range(2000)], "@sleep 1.2")

    started = time.monotonic()
    result, seen = _call_with_progress(_app(server, "build"), "build", {"env": "tbeam"})
    elapsed = time.monotonic() - started

    assert result.is_error is False
    assert 1 <= len(seen) <= elapsed / jobs.PROGRESS_INTERVAL_S + 1, (elapsed, seen)


def test_a_hardware_tool_reports_esptool_v4_progress(server, scripted, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    monkeypatch.setattr(config, "esptool_bin", lambda: Path("/nonexistent/esptool"))
    asked = scripted(
        "Flash will be erased from 0x00010000 to 0x0001ffff...",
        "Compressed 65536 bytes to 21000...",
        "Writing at 0x00010000... (25 %)",
        _HOLD,
        "Writing at 0x00014000... (50 %)",
        _HOLD,
        "Writing at 0x00018000... (75 %)",
        _HOLD,
        "Wrote 65536 bytes (21000 compressed) at 0x00010000 in 2.1 seconds",
        "Hash of data verified.",
    )

    result, seen = _call_with_progress(
        _app(server, "esptool_raw"),
        "esptool_raw",
        {"args": ["write_flash", "0x10000", "fw.bin"], "port": PORT, "confirm": True},
    )

    assert result.is_error is False
    assert asked[0][0] == "/nonexistent/esptool"
    writes = [(p, t) for p, t, m in seen if m and m.startswith("Writing at")]
    assert [round(p / t, 2) for p, t in writes[:3]] == [0.25, 0.5, 0.75], seen
    assert _strictly_rising(seen)


def test_a_client_without_a_progress_token_gets_a_clean_result(
    server, scripted, firmware_tree, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    monkeypatch.setattr(config, "esptool_bin", lambda: Path("/nonexistent/esptool"))
    scripted(*ESPTOOL_V5_UPLOAD)
    app = _app(server, "pio_flash", "esptool_raw")

    async def run():
        async with Client(app) as client:
            # The SDK session call sends no progress token when no callback is given.
            flashed = await client.session.call_tool(
                "pio_flash", {"env": "heltec-v3", "port": PORT, "confirm": True}
            )
            raw = await client.session.call_tool(
                "esptool_raw",
                {"args": ["write_flash", "0x10000", "fw.bin"], "port": PORT, "confirm": True},
            )
            return flashed, raw

    flashed, raw = asyncio.run(run())
    assert flashed.is_error is False, flashed.content
    assert flashed.structured_content["exit_code"] == 0
    assert raw.is_error is False, raw.content


def test_a_build_task_shows_progress_as_its_status_message(
    server, scripted, firmware_tree, monkeypatch
) -> None:
    pytest.importorskip("fastmcp_tasks")
    from fastmcp_tasks import TasksExtension, call_tool_task

    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.2, raising=False)
    scripted(*COMPILE)
    app = _app(server, "build", task=True)
    app.add_extension(TasksExtension())

    async def run():
        async with Client(app) as client:
            task = await call_tool_task(client, "build", {"env": "tbeam"})
            messages = []
            for _ in range(80):
                status = await task.status()
                if status.status_message:
                    messages.append(status.status_message)
                if status.status != "working":
                    break
                await asyncio.sleep(0.1)
            return messages, await task.result()

    messages, result = asyncio.run(run())
    assert result.is_error is False
    assert any(m.startswith("Compiling ") for m in messages), messages
