# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Mounting and ejecting the UF2 drive from the host, and the tools on top."""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ElicitResult

from meshtastic_mcp import confirm, uf2_host
from tests.unit._tool_fns import tool_fn

DRIVE_PATH = "/org/freedesktop/UDisks2/drives/Mesh_Meshtastic_UF2_7885014CB2C5B186"


def _runner(fail_on: str | None = None, message: str = ""):
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        if fail_on and fail_on in argv:
            return subprocess.CompletedProcess(argv, 1, "", message)
        if argv[:2] == ["udisksctl", "info"]:
            return subprocess.CompletedProcess(argv, 0, f"  Drive:  '{DRIVE_PATH}'\n", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    return run, calls


@pytest.mark.parametrize(
    "link",
    [
        "usb-Adafruit_nRF_UF2_7885014CB2C5B186-0:0",
        "usb-Mesh_Meshtastic_UF2_7885014CB2C5B186-0:0",
    ],
)
def test_either_bootloaders_drive_name_resolves_by_serial(tmp_path, link) -> None:
    dev = tmp_path / "sdb"
    dev.write_text("")
    (tmp_path / "by-id").mkdir()
    (tmp_path / "by-id" / link).symlink_to(dev)
    found = uf2_host.block_device_for_serial("7885014cb2c5b186", tmp_path / "by-id")
    assert found == str(dev.resolve())


def test_linux_eject_sends_the_udisks_drive_eject() -> None:
    run, calls = _runner()
    uf2_host.eject("/dev/sdb", None, platform="linux", run=run)
    assert calls[-1][0] == "gdbus" and "org.freedesktop.UDisks2.Drive.Eject" in calls[-1]
    assert DRIVE_PATH in calls[-1]


def test_a_polkit_refusal_names_the_actions_to_grant() -> None:
    run, _ = _runner(
        fail_on="gdbus", message="GDBus.Error:org.freedesktop.UDisks2.Error.NotAuthorized"
    )
    with pytest.raises(uf2_host.NotAuthorized) as err:
        uf2_host.eject("/dev/sdb", None, platform="linux", run=run)
    text = str(err.value)
    assert "eject-media-other-seat" in text and "sudo tee /etc/polkit-1/rules.d/" in text
    assert "filesystem-mount" not in text  # only the actions eject needs


def test_a_refused_mount_asks_only_for_the_mount_actions() -> None:
    run, _ = _runner(fail_on="mount", message="Error: Not authorized to perform operation")
    with pytest.raises(uf2_host.NotAuthorized) as err:
        uf2_host.mount("/dev/sdb", platform="linux", run=run)
    assert "filesystem-mount-other-seat" in str(err.value)
    assert "eject-media" not in str(err.value)


def test_doctor_reports_udisks_on_linux_only(monkeypatch) -> None:
    from meshtastic_mcp import doctor

    monkeypatch.setattr(doctor, "_IS_LINUX", False)
    assert doctor._udisks_check() is None


def test_doctor_degrades_with_the_rule_when_polkit_refuses(monkeypatch) -> None:
    from meshtastic_mcp import doctor

    monkeypatch.setattr(doctor, "_IS_LINUX", True)
    monkeypatch.setattr(doctor, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "Not authorized"),
    )
    check = doctor._udisks_check()
    assert check is not None and check.status == doctor.STATUS_DEGRADED
    assert check.fix.startswith("sudo tee /etc/polkit-1/rules.d/")


def test_doctor_is_ok_when_the_session_may_mount_and_eject(monkeypatch) -> None:
    from meshtastic_mcp import doctor

    monkeypatch.setattr(doctor, "_IS_LINUX", True)
    monkeypatch.setattr(doctor, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")
    )
    check = doctor._udisks_check()
    assert check is not None and check.status == doctor.STATUS_OK and not check.fix


def test_macos_mount_and_eject_use_diskutil() -> None:
    run, calls = _runner()
    uf2_host.mount("disk4", platform="darwin", run=run)
    uf2_host.eject(None, "/Volumes/RAK4631", platform="darwin", run=run)
    assert calls == [["diskutil", "mount", "disk4"], ["diskutil", "eject", "/Volumes/RAK4631"]]


@pytest.fixture(scope="module")
def server():
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name, args, handler=None):
    app = FastMCP("t")
    tool = tool_fn(name)
    app.tool()(getattr(tool, "fn", tool))

    async def run():
        async with Client(app, elicitation_handler=handler) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def test_eject_asks_first_and_reports_a_polkit_refusal(server, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(confirm.POLICY_ENV, "agent")
    drive = {"mount": str(tmp_path), "label": "RAK4631", "device": "/dev/sdb"}
    monkeypatch.setattr("meshtastic_mcp.tools.hardware._uf2_drive", lambda port: drive)

    def refuse(device, mount_point):
        raise uf2_host.NotAuthorized("polkit refused: grant eject-media-other-seat")

    monkeypatch.setattr(uf2_host, "eject", refuse)
    seen: list[str] = []

    async def accept(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept", content={"confirm": True})

    result = _call(server, "nrf52_eject_uf2", {}, accept)
    assert len(seen) == 1 and "eject" in seen[0]
    assert result.is_error is True and "eject-media-other-seat" in result.content[0].text


def test_mount_finds_the_device_by_the_ports_serial(server, monkeypatch, tmp_path) -> None:
    drive = {"mount": str(tmp_path), "label": "RAK4631", "device": "/dev/sdb"}
    state = {"mounted": False}

    def find(port):
        from fastmcp.exceptions import ToolError

        if not state["mounted"]:
            raise ToolError("no UF2 drive")
        return drive

    monkeypatch.setattr("meshtastic_mcp.tools.hardware._uf2_drive", find)
    monkeypatch.setattr(uf2_host, "block_device_for_serial", lambda serial: f"/dev/by/{serial}")
    mounted: list[str] = []

    def do_mount(dev):
        mounted.append(dev)
        state["mounted"] = True
        return "Mounted"

    monkeypatch.setattr(uf2_host, "mount", do_mount)
    port = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_7885014CB2C5B186-if00"
    result = _call(server, "nrf52_mount_uf2", {"port": port})
    assert result.is_error is False, result.content[0].text
    assert mounted == ["/dev/by/7885014CB2C5B186"]
    assert result.structured_content["mounted"] is True
