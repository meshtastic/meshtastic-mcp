# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`install`: the board picks the flasher, and the result says what the board now runs."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from meshtastic_mcp import boards, connection, flash, hw_tools
from meshtastic_mcp.tools import hardware

PORT = "/dev/ttyUSB0"
OK = {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}


@pytest.fixture
def bench(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def record(name):
        def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return dict(OK)

        return fn

    monkeypatch.setattr(flash, "flash", record("flash"))
    monkeypatch.setattr(flash, "update_flash", record("update_flash"))
    monkeypatch.setattr(flash, "erase_and_flash", record("erase_and_flash"))
    monkeypatch.setattr(hw_tools, "nrfutil_dfu", record("nrfutil_dfu"))
    monkeypatch.setattr(hw_tools, "esptool_raw", record("esptool_raw"))
    archs = {"heltec-v3": "esp32s3", "rak4631": "nrf52840", "pico": "rp2040"}
    monkeypatch.setattr(boards, "get_board", lambda env: {"architecture": archs[env]})

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        yield SimpleNamespace(metadata=SimpleNamespace(firmware_version="2.8.3.abc"))

    monkeypatch.setattr(connection, "connect", fake_connect)
    return SimpleNamespace(calls=calls, tmp=tmp_path)


@pytest.mark.parametrize(
    ("env", "erase", "method"),
    [
        ("heltec-v3", False, "update_flash"),
        ("heltec-v3", True, "erase_and_flash"),
        ("rak4631", False, "flash"),
        ("pico", False, "flash"),
    ],
)
def test_a_build_goes_to_the_flasher_its_board_needs(bench, env, erase, method) -> None:
    result = hardware._install(PORT, {"env": env}, erase, None, None)
    assert [c[0] for c in bench.calls] == [method]
    assert result["back"] is True and result["firmware_version"] == "2.8.3.abc"


def test_erase_is_refused_for_a_board_that_is_not_esp32(bench) -> None:
    with pytest.raises(ToolError, match="ESP32"):
        hardware._install(PORT, {"env": "rak4631"}, True, None, None)
    assert bench.calls == []


@pytest.mark.parametrize(("ext", "method"), [(".zip", "nrfutil_dfu"), (".bin", "esptool_raw")])
def test_a_file_goes_by_its_type(bench, ext, method) -> None:
    path = bench.tmp / f"firmware{ext}"
    path.write_bytes(b"x")
    hardware._install(PORT, {"file": str(path)}, False, None, None)
    assert [c[0] for c in bench.calls] == [method]


def test_a_failed_write_does_not_wait_for_the_board(bench, monkeypatch) -> None:
    monkeypatch.setattr(flash, "flash", lambda *a, **k: {"exit_code": 1, "stderr_tail": "boom"})
    result = hardware._install(PORT, {"env": "rak4631"}, False, None, None)
    assert result["exit_code"] == 1 and "back" not in result


def test_an_unknown_file_type_is_refused(bench) -> None:
    path = bench.tmp / "firmware.hex"
    path.write_bytes(b"x")
    with pytest.raises(ToolError, match=r"\.uf2, \.zip or \.bin"):
        hardware._install(PORT, {"file": str(path)}, False, None, None)
