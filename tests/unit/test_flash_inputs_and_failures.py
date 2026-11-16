# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`erase_and_flash` finds the inputs its script needs, and failed flashes are tool errors."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from meshtastic_mcp import config, flash, hw_tools, pio


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A firmware root with a built heltec-v3 env: factory image, manifest, littlefs."""
    root = tmp_path / "firmware"
    build = root / ".pio" / "build" / "heltec-v3"
    build.mkdir(parents=True)
    (build / "firmware-heltec-v3-1.0.factory.bin").write_bytes(b"f")
    (build / "littlefs-heltec-v3-1.0.bin").write_bytes(b"l")
    (root / "mt-esp32s3-ota.bin").write_bytes(b"ota")
    monkeypatch.setattr(config, "firmware_root", lambda: root)
    monkeypatch.setattr(flash, "_check_esp32_env", lambda env: "esp32s3")
    return root, build


def _write_meta(build, mcu="esp32s3"):
    (build / "firmware-heltec-v3-1.0.mt.json").write_text(
        json.dumps({"mcu": mcu}), encoding="utf-8"
    )


def _factory(build):
    return build / "firmware-heltec-v3-1.0.factory.bin"


def test_the_ota_image_is_staged_beside_the_factory_image(tree) -> None:
    _, build = tree
    _write_meta(build)
    flash._stage_install_inputs(_factory(build))
    assert (build / "mt-esp32s3-ota.bin").read_bytes() == b"ota"


def test_an_ota_image_already_in_the_build_dir_is_left_alone(tree) -> None:
    _, build = tree
    _write_meta(build)
    (build / "mt-esp32s3-ota.bin").write_bytes(b"newer")
    flash._stage_install_inputs(_factory(build))
    assert (build / "mt-esp32s3-ota.bin").read_bytes() == b"newer"


def test_a_missing_ota_image_is_a_clear_error(tree) -> None:
    root, build = tree
    _write_meta(build)
    (root / "mt-esp32s3-ota.bin").unlink()
    with pytest.raises(flash.FlashError, match="unified OTA image"):
        flash._stage_install_inputs(_factory(build))


def test_staging_needs_the_manifest(tree) -> None:
    _, build = tree
    with pytest.raises(flash.FlashError, match=r"mt\.json is missing"):
        flash._stage_install_inputs(_factory(build))


def test_a_factory_image_without_its_manifest_is_rebuilt_not_trusted(tree, monkeypatch) -> None:
    root, build = tree
    builds: list[list[str]] = []

    def fake_run(args, **_kw):
        builds.append(list(args))
        _write_meta(build)
        return SimpleNamespace(returncode=0, stdout="", stderr="", duration_s=0.1)

    monkeypatch.setattr(pio, "run", fake_run)
    (root / "bin").mkdir()
    (root / "bin" / "device-install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(config, "esptool_bin", lambda: root / "esptool")
    monkeypatch.setattr(
        flash,
        "_run_install_script",
        lambda script, port, factory: {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""},
    )
    flash.erase_and_flash("heltec-v3", "/dev/x", confirm=True)
    assert builds == [["run", "-e", "heltec-v3", "-t", "mtjson"]]
    assert (build / "mt-esp32s3-ota.bin").is_file()


def test_skip_build_refuses_a_factory_image_without_its_manifest(tree, monkeypatch) -> None:
    root, _ = tree
    (root / "bin").mkdir()
    (root / "bin" / "device-install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(flash.FlashError, match=r"mt\.json found"):
        flash.erase_and_flash("heltec-v3", "/dev/x", confirm=True, skip_build=True)


# ---- failures are tool errors ----------------------------------------------------


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name, args):
    app = FastMCP("t")
    tool = getattr(server, name)
    app.tool()(getattr(tool, "fn", tool))

    async def run():
        async with Client(app) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


@pytest.mark.parametrize("tool", ["pio_flash", "erase_and_flash", "update_flash"])
def test_a_nonzero_exit_is_a_tool_error_with_the_output_tail(server, monkeypatch, tool) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", "/tmp/mcp-test-data")
    failing = {"exit_code": 1, "stdout_tail": "", "stderr_tail": "esptool: no chip found"}
    target = {
        "pio_flash": "flash",
        "erase_and_flash": "erase_and_flash",
        "update_flash": "update_flash",
    }
    monkeypatch.setattr(flash, target[tool], lambda *a, **kw: dict(failing))
    result = _call(server, tool, {"env": "e", "port": "/dev/x", "confirm": True})
    assert result.is_error is True
    assert "exit code 1" in result.content[0].text and "no chip found" in result.content[0].text


def test_a_flash_to_the_wrong_port_is_a_tool_error(server, monkeypatch) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", "/tmp/mcp-test-data")
    wrong = {
        "exit_code": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "upload_port_mismatch": "wrote /dev/y",
    }
    monkeypatch.setattr(flash, "flash", lambda *a, **kw: dict(wrong))
    result = _call(server, "pio_flash", {"env": "e", "port": "/dev/x", "confirm": True})
    assert result.is_error is True and "wrote /dev/y" in result.content[0].text


def test_a_successful_flash_is_not_an_error(server, monkeypatch) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", "/tmp/mcp-test-data")
    monkeypatch.setattr(flash, "flash", lambda *a, **kw: {"exit_code": 0, "stdout_tail": "ok"})
    result = _call(server, "pio_flash", {"env": "e", "port": "/dev/x", "confirm": True})
    assert result.is_error is False


def test_a_failing_hardware_tool_is_a_tool_error(server, monkeypatch) -> None:
    monkeypatch.setattr(hw_tools.config, "esptool_bin", lambda: "esptool")
    monkeypatch.setattr(
        hw_tools,
        "_run",
        lambda binary, args, **_kw: {
            "exit_code": 2,
            "stdout_tail": "",
            "stderr_tail": "no serial port",
        },
    )
    result = _call(server, "esptool_raw", {"args": ["flash_id"]})
    assert result.is_error is True and "no serial port" in result.content[0].text


# ---- nrfutil_dfu: adafruit-nrfutil exits 0 on failure ------------------------------


@pytest.fixture
def adafruit(monkeypatch, tmp_path):
    pkg = tmp_path / "firmware.zip"
    pkg.write_bytes(b"zip")
    runs: list[list[str]] = []
    out = {"stdout": ""}
    monkeypatch.setattr(hw_tools.config, "nrfutil_bin", lambda: tmp_path / "adafruit-nrfutil")

    def fake_run(binary, args, **_kw):
        runs.append(list(args))
        return {"exit_code": 0, "stdout": out["stdout"], "stderr": "", "stdout_tail": out["stdout"]}

    monkeypatch.setattr(hw_tools, "_run", fake_run)
    return pkg, runs, out


def test_adafruit_dfu_touches_and_uses_single_bank_in_one_command(adafruit) -> None:
    pkg, runs, out = adafruit
    out["stdout"] = "Upgrading target...\nDevice programmed.\n"
    res = hw_tools.nrfutil_dfu("/dev/x", str(pkg), confirm=True)
    assert res["exit_code"] == 0
    assert runs[0][-3:] == ["--singlebank", "-t", "1200"]


def test_adafruit_dfu_that_never_programmed_is_a_failure(adafruit) -> None:
    pkg, _, out = adafruit
    out["stdout"] = "Failed to upgrade target. Error is: No data received on serial port.\n"
    assert hw_tools.nrfutil_dfu("/dev/x", str(pkg), confirm=True)["exit_code"] == 1


def test_nordic_nrfutil_keeps_its_own_arguments(monkeypatch, tmp_path) -> None:
    pkg = tmp_path / "firmware.zip"
    pkg.write_bytes(b"zip")
    runs: list[list[str]] = []
    monkeypatch.setattr(hw_tools.config, "nrfutil_bin", lambda: tmp_path / "nrfutil")
    monkeypatch.setattr(
        hw_tools,
        "_run",
        lambda b, args, **_kw: runs.append(list(args)) or {"exit_code": 0, "stdout": ""},
    )
    assert hw_tools.nrfutil_dfu("/dev/x", str(pkg), confirm=True)["exit_code"] == 0
    assert "--singlebank" not in runs[0] and "-t" not in runs[0]


def test_adafruit_dfu_resolves_a_by_id_link_to_its_device(adafruit, tmp_path) -> None:
    pkg, runs, out = adafruit
    out["stdout"] = "Device programmed.\n"
    device = tmp_path / "ttyACM0"
    device.write_text("")
    link = tmp_path / "usb-RAKwireless_WisCore_RAK4631_Board-if00"
    link.symlink_to(device)
    hw_tools.nrfutil_dfu(str(link), str(pkg), confirm=True)
    port_arg = runs[0][runs[0].index("--port") + 1]
    assert port_arg == str(device)
