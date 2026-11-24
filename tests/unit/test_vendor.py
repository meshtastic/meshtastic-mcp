# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`vendor`: one pass-through for the flash tools, asking only before a write."""

from __future__ import annotations

import asyncio

import pytest

from meshtastic_mcp import confirm, hw_tools
from meshtastic_mcp.tools import hardware

OK = {"exit_code": 0, "stdout_tail": "ok", "stderr_tail": ""}


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []

    def esptool_raw(args, port=None, confirm=False):
        seen.append(("esptool", args, port, confirm))
        if "write_flash" in args and not confirm:
            raise hw_tools.ConfirmRequired("esptool write_flash")
        return dict(OK)

    def nrfutil_raw(args, confirm=False):
        seen.append(("nrfutil", args, None, confirm))
        return dict(OK)

    monkeypatch.setattr(hw_tools, "esptool_raw", esptool_raw)
    monkeypatch.setattr(hw_tools, "nrfutil_raw", nrfutil_raw)
    return seen


def _run(**kwargs):
    return asyncio.run(hardware.vendor(None, **kwargs))


def test_a_read_runs_without_asking(calls, monkeypatch) -> None:
    async def never(*a, **k):
        raise AssertionError("a read is not gated")

    monkeypatch.setattr(confirm, "gate", never)
    assert _run(tool="esptool", args=["chip_id"], port="/dev/ttyUSB0")["exit_code"] == 0
    assert calls == [("esptool", ["chip_id"], "/dev/ttyUSB0", False)]


def test_a_write_asks_first_and_runs_once_agreed(calls, monkeypatch) -> None:
    asked = []

    async def agree(ctx, what, confirmed, irreversible=False):
        asked.append((what, irreversible))
        return None

    monkeypatch.setattr(confirm, "gate", agree)
    _run(tool="esptool", args=["write_flash", "0x0", "fw.bin"], port="/dev/ttyUSB0")
    assert asked == [("run esptool write_flash", True)]
    assert [c[3] for c in calls] == [False, True]


def test_each_tool_goes_to_its_own_runner(calls) -> None:
    _run(tool="nrfutil", args=["version"])
    assert calls == [("nrfutil", ["version"], None, False)]


@pytest.mark.parametrize(
    ("read_only", "value_flags", "depth", "args"),
    [
        (hw_tools.ESPTOOL_READ_ONLY, set(), 1, [word])
        for word in ("erase_flash", "erase-flash", "ERASE_FLASH", "Erase-Flash", "burn-efuse")
    ]
    + [
        (hw_tools.NRFUTIL_READ_ONLY, set(), 2, ["device", word])
        for word in ("erase", "ERASE", "recover", "Recover")
    ]
    + [
        (hw_tools.PICOTOOL_READ_ONLY, set(), 1, [word])
        for word in ("erase", "ERASE", "otp", "load")
    ],
)
def test_every_spelling_of_a_write_asks(read_only, value_flags, depth, args):
    assert hw_tools._unsafe_call(args, read_only, value_flags, depth) is not None


@pytest.mark.parametrize("word", ["chip_id", "chip-id", "CHIP_ID", "Flash-Id"])
def test_every_spelling_of_a_read_runs(word):
    assert hw_tools._unsafe_call([word], hw_tools.ESPTOOL_READ_ONLY, set()) is None
