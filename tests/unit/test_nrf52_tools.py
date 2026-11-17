# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The nRF52 bootloader tools: drive matching, the SoftDevice check, and gating."""

from __future__ import annotations

import asyncio
import struct

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ElicitResult

from meshtastic_mcp import admin, confirm, nrf52_bootloader

INFO_OTAFIX = (
    "UF2 Bootloader 2.6.0\r\nModel: WisBlock RAK4631 Board\r\nBoard-ID: WisBlock-RAK4631-Board\r\n"
    "Date: Nov  1 2026\r\nFactory-Erase: UF2 family 0x4D455348\r\nSoftDevice: S140 6.1.1\r\n"
)
INFO_OLD = (
    "UF2 Bootloader 0.6.1\r\nModel: WisBlock RAK4631 Board\r\nBoard-ID: WisBlock-RAK4631-Board\r\n"
    "Date: Jan  1 2022\r\nSoftDevice: S140 6.1.1\r\n"
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(confirm.POLICY_ENV, raising=False)


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _drive(tmp_path, info_text):
    mount = tmp_path / "RAK4631"
    mount.mkdir()
    (mount / "INFO_UF2.TXT").write_text(info_text)
    return {
        "mount": str(mount),
        "label": "RAK4631",
        "info": nrf52_bootloader.parse_info_uf2(info_text),
    }


def _uf2(path, target):
    block = struct.pack("<IIIIIIII", 0x0A324655, 0x9E5D5157, 0x2000, target, 256, 0, 1, 0xADA52840)
    block += b"\0" * 476 + struct.pack("<I", 0x0AB16F30)
    path.write_bytes(block)
    return str(path)


def _app(server, *names):
    app = FastMCP("t")
    for name in names:
        tool = getattr(server, name)
        app.tool()(getattr(tool, "fn", tool))
    return app


def _call(app, name, args, handler=None):
    async def run():
        async with Client(app, elicitation_handler=handler) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def _accept():
    seen: list[str] = []

    async def handler(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept", content={"confirm": True})

    return handler, seen


def test_a_gone_app_port_still_finds_its_drive_by_serial(server, monkeypatch, tmp_path) -> None:
    drive = _drive(tmp_path, INFO_OTAFIX)
    monkeypatch.setattr(nrf52_bootloader, "find_uf2_drives", lambda: {"drives": [drive]})
    asked: dict = {}

    def fake_match(port=None, serial_number=None, drives=None):
        asked.update(port=port, serial_number=serial_number)
        return {"ok": True, "drive": drive}

    monkeypatch.setattr(nrf52_bootloader, "find_drive_for_port", fake_match)
    gone = str(tmp_path / "usb-RAKwireless_WisCore_RAK4631_Board_7885014CB2C5B186-if00")
    assert server._uf2_drive(gone)["label"] == "RAK4631"
    assert asked == {"port": None, "serial_number": "7885014CB2C5B186"}


def test_an_image_for_the_other_softdevice_is_refused_before_asking(
    server, monkeypatch, tmp_path
) -> None:
    drive = _drive(tmp_path, INFO_OTAFIX)
    monkeypatch.setattr(server, "_uf2_drive", lambda port: drive)
    monkeypatch.setattr(nrf52_bootloader, "flash_uf2", lambda *a, **k: pytest.fail("flashed"))
    handler, seen = _accept()
    image = _uf2(tmp_path / "s140v7.uf2", 0x27000)
    result = _call(_app(server, "nrf52_flash_uf2"), "nrf52_flash_uf2", {"uf2_path": image}, handler)
    assert result.is_error is True and "other SoftDevice" in result.content[0].text
    assert seen == []


def test_a_matching_image_asks_then_flashes_as_a_job(server, monkeypatch, tmp_path) -> None:
    drive = _drive(tmp_path, INFO_OTAFIX)
    monkeypatch.setattr(server, "_uf2_drive", lambda port: drive)
    monkeypatch.setattr(
        nrf52_bootloader, "flash_uf2", lambda d, p, timeout_s: {"ok": True, "outcome": "detached"}
    )
    handler, seen = _accept()
    image = _uf2(tmp_path / "s140v6.uf2", 0x26000)
    result = _call(_app(server, "nrf52_flash_uf2"), "nrf52_flash_uf2", {"uf2_path": image}, handler)
    assert result.is_error is False and len(seen) == 1
    assert result.structured_content["job_id"]


def test_a_copy_the_bootloader_did_not_take_is_an_error(server, monkeypatch, tmp_path) -> None:
    drive = _drive(tmp_path, INFO_OTAFIX)
    monkeypatch.setattr(server, "_uf2_drive", lambda port: drive)
    monkeypatch.setattr(
        nrf52_bootloader,
        "flash_uf2",
        lambda d, p, timeout_s: {"ok": True, "outcome": "still_present"},
    )
    image = _uf2(tmp_path / "s140v6.uf2", 0x26000)
    args = {"uf2_path": image, "confirm": True}
    assert _call(_app(server, "nrf52_flash_uf2"), "nrf52_flash_uf2", args).is_error is True


def test_factory_erase_refuses_a_bootloader_without_it(server, monkeypatch, tmp_path) -> None:
    drive = _drive(tmp_path, INFO_OLD)
    monkeypatch.setattr(server, "_uf2_drive", lambda port: drive)
    handler, seen = _accept()
    result = _call(_app(server, "nrf52_factory_erase"), "nrf52_factory_erase", {}, handler)
    assert result.is_error is True and "does not accept" in result.content[0].text
    assert seen == []


def test_factory_erase_asks_then_writes_the_erase_file(server, monkeypatch, tmp_path) -> None:
    drive = _drive(tmp_path, INFO_OTAFIX)
    monkeypatch.setattr(server, "_uf2_drive", lambda port: drive)
    written: list[bytes] = []

    def fake_erase(d, path):
        written.append(path.read_bytes())
        return {"ok": True, "outcome": "detached"}

    monkeypatch.setattr(nrf52_bootloader, "factory_erase", fake_erase)
    handler, seen = _accept()
    result = _call(_app(server, "nrf52_factory_erase"), "nrf52_factory_erase", {}, handler)
    assert result.is_error is False and len(seen) == 1
    assert written == [nrf52_bootloader.factory_erase_uf2_bytes()]


def test_enter_uf2_sends_nothing_without_a_confirmation(server, monkeypatch) -> None:
    monkeypatch.setattr(admin, "enter_uf2_mode", lambda **k: pytest.fail("sent"))
    app = _app(server, "nrf52_enter_uf2")
    result = _call(app, "nrf52_enter_uf2", {"port": "/dev/x"})
    assert result.is_error is True and "confirm=True" in result.content[0].text
