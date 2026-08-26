# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the upload-port pinning and post-flash port verification.

Real incident (2026-08-25): pioarduino's Hybrid-Compile pass re-invoked a
child `pio run -e meshnology_w10 -t upload` without forwarding
`--upload-port`, PlatformIO auto-detected a different bench device, and a
16MB-partition image landed on an 8MB Heltec Wireless Tracker V2 —
boot-looping it while the flash job reported success. These tests pin the
three defenses: reject ports PlatformIO would ignore, export
`PLATFORMIO_UPLOAD_PORT` so child pio runs inherit the port, and fail the
job loudly when the output shows a different port was used.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from meshtastic_mcp import config, flash, pio, port_recovery, registry

REQUESTED = "/dev/cu.usbmodem1201"
WRONG = "/dev/cu.usbmodem13201"

# Condensed from the incident log (flashes/a4170dbb5089.log): the hybrid
# child run auto-detects, esptool then names the port it actually used.
_WRONG_PORT_STDOUT = (
    "*** Compile Arduino IDF libs for meshnology_w10 ***\n"
    "Configuring upload protocol...\n"
    "CURRENT: upload_protocol = esptool\n"
    "Looking for upload port...\n"
    f"Auto-detected: {WRONG}\n"
    "Uploading .pio/build/meshnology_w10/firmware.bin\n"
    "esptool v5.3.0\n"
    f"Serial port {WRONG}:\n"
    "Hash of data verified.\n"
    "========================= [SUCCESS] Took 1571.05 seconds ==============\n"
)


# --- _require_port ----------------------------------------------------------


@pytest.mark.parametrize("bad_port", ["", "   ", "/dev/cu.usbmodem*", "/dev/cu.usb?[12]"])
def test_flash_start_rejects_ports_platformio_would_ignore(bad_port: str) -> None:
    """An empty port makes `pio run --upload-port` auto-detect; a glob makes
    it pattern-match. Both can flash a different device — refuse up front."""
    with pytest.raises(flash.FlashError):
        flash.flash_start("meshnology_w10", bad_port, confirm=True)


def test_all_flash_entrypoints_reject_empty_port() -> None:
    for fn in (flash.flash, flash.flash_start, flash.erase_and_flash, flash.update_flash):
        with pytest.raises(flash.FlashError):
            fn("meshnology_w10", "", confirm=True)


# --- _verify_upload_port ----------------------------------------------------


def test_verify_flags_the_incident_output() -> None:
    msg = flash._verify_upload_port(REQUESTED, _WRONG_PORT_STDOUT, "")
    assert msg is not None
    assert WRONG in msg and REQUESTED in msg


def test_verify_accepts_matching_ports() -> None:
    ok = f"Using manually specified: {REQUESTED}\nesptool v5.3.0\nSerial port {REQUESTED}:\n"
    assert flash._verify_upload_port(REQUESTED, ok, "") is None


def test_verify_ignores_output_naming_no_port() -> None:
    # nRF52 DFU output names no port in the recognized formats; the check
    # must stay silent rather than false-positive.
    nrf = (
        "Forcing reset using 1200bps open/close on port /dev/cu.usbmodem143101\n"
        "Uploading firmware.zip\nDevice programmed.\n"
    )
    assert flash._verify_upload_port(REQUESTED, nrf, "") is None


def test_upload_port_env_merges_build_flags() -> None:
    env = flash._upload_port_env(REQUESTED, {"DEBUG_HEAP": True})
    assert env["PLATFORMIO_UPLOAD_PORT"] == REQUESTED
    assert "-DDEBUG_HEAP" in env["PLATFORMIO_BUILD_FLAGS"]
    assert flash._upload_port_env(REQUESTED, None) == {"PLATFORMIO_UPLOAD_PORT": REQUESTED}


# --- flash(): pinning + loud failure ----------------------------------------


class _Result:
    def __init__(self, stdout: str, returncode: int = 0):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""
        self.duration_s = 1.0


@pytest.mark.firmware
def test_flash_pins_port_via_cli_and_env() -> None:
    captured: dict = {}

    def _stub_run(args, **kwargs):
        captured["args"] = args
        captured["extra_env"] = kwargs.get("extra_env")
        return _Result(f"Using manually specified: {REQUESTED}\n")

    with (
        patch.object(port_recovery, "ensure_port_free", return_value=REQUESTED),
        patch.object(pio, "run", side_effect=_stub_run),
    ):
        out = flash.flash("meshnology_w10", REQUESTED, confirm=True)

    args = captured["args"]
    assert args[args.index("--upload-port") + 1] == REQUESTED
    # The env var is what survives into pioarduino's Hybrid-Compile child run.
    assert captured["extra_env"]["PLATFORMIO_UPLOAD_PORT"] == REQUESTED
    assert out["exit_code"] == 0
    assert "upload_port_mismatch" not in out


@pytest.mark.firmware
def test_flash_fails_loudly_when_wrong_port_was_flashed() -> None:
    with (
        patch.object(port_recovery, "ensure_port_free", return_value=REQUESTED),
        patch.object(pio, "run", return_value=_Result(_WRONG_PORT_STDOUT)),
    ):
        out = flash.flash("meshnology_w10", REQUESTED, confirm=True)

    assert out["exit_code"] != 0, "a wrong-device flash must not look like success"
    assert WRONG in out["upload_port_mismatch"]


@pytest.mark.firmware
def test_flash_start_job_fails_on_port_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))

    with patch.object(pio, "run", return_value=_Result(_WRONG_PORT_STDOUT)):
        started = flash.flash_start("meshnology_w10", REQUESTED, confirm=True)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            polled = flash.flash_poll(started["job_id"])
            if polled["status"] != "running":
                break
            time.sleep(0.05)

    assert polled["status"] == "failed"
    assert polled["exit_code"] != 0
    assert WRONG in polled["error"]
    # The failure must also be visible in the job log an operator tails.
    assert "flash job FAILED" in "\n".join(polled["log_tail"])


# --- per-port serialization -------------------------------------------------
#
# An upload is the one device operation that must not share a port. Two
# concurrent flashes drive the same serial line, and — worse — an in-process
# `connect()` holding the port makes `ensure_port_free` read the device as
# wedged and power-cycle its hub slot, yanking VBUS out from under a flash in
# progress. Uploads take the same non-blocking `registry.port_lock` the
# connect/serial paths use, so they all serialize against each other.


def test_flash_refuses_a_port_another_operation_holds() -> None:
    """And refuses BEFORE the pre-flight: ensure_port_free would treat the
    in-process holder as a wedged device and power-cycle the hub."""
    lock = registry.port_lock(REQUESTED)
    assert lock.acquire(blocking=False)
    try:
        with (
            patch.object(port_recovery, "ensure_port_free") as recover,
            patch.object(pio, "run") as run,
            pytest.raises(flash.FlashError, match="busy"),
        ):
            flash.flash("meshnology_w10", REQUESTED, confirm=True)
        recover.assert_not_called()
        run.assert_not_called()
    finally:
        lock.release()
        registry.clear_port_lock(REQUESTED)


def test_flash_refuses_a_port_a_serial_session_holds_and_frees_the_lock() -> None:
    """`serial_open` registers its session while holding this same lock, so the
    session check has to come after the acquire — and rejecting must not keep it."""
    session = SimpleNamespace(id="ses-1")

    def _session_only_if_lock_is_held(port: str):
        probe = registry.port_lock(port)
        if probe.acquire(blocking=False):
            # Checked before the acquire — the window serial_open registers in.
            probe.release()
            return None
        return session

    try:
        with (
            patch.object(
                registry, "active_session_for_port", side_effect=_session_only_if_lock_is_held
            ),
            patch.object(port_recovery, "ensure_port_free") as recover,
            pytest.raises(flash.FlashError, match="serial session ses-1"),
        ):
            flash.flash("meshnology_w10", REQUESTED, confirm=True)
        recover.assert_not_called()

        lock = registry.port_lock(REQUESTED)
        assert lock.acquire(blocking=False), "the rejected flash kept the port lock"
        lock.release()
    finally:
        registry.clear_port_lock(REQUESTED)


def test_flash_start_fails_fast_when_the_port_is_busy(tmp_path, monkeypatch) -> None:
    """The lock is taken on the caller's thread, so a busy port is an error
    now — not a job the caller only discovers is dead on the first poll."""
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    lock = registry.port_lock(REQUESTED)
    assert lock.acquire(blocking=False)
    try:
        with patch.object(pio, "run") as run, pytest.raises(flash.FlashError, match="busy"):
            flash.flash_start("meshnology_w10", REQUESTED, confirm=True)
        run.assert_not_called()
    finally:
        lock.release()
        registry.clear_port_lock(REQUESTED)


@pytest.mark.firmware
def test_flash_releases_the_port_lock_on_both_paths() -> None:
    try:
        with (
            patch.object(port_recovery, "ensure_port_free", return_value=REQUESTED),
            patch.object(pio, "run", return_value=_Result(_WRONG_PORT_STDOUT)),
        ):
            flash.flash("meshnology_w10", REQUESTED, confirm=True)
        with (
            patch.object(port_recovery, "ensure_port_free", return_value=REQUESTED),
            patch.object(pio, "run", side_effect=RuntimeError("pio blew up")),
            pytest.raises(RuntimeError),
        ):
            flash.flash("meshnology_w10", REQUESTED, confirm=True)

        lock = registry.port_lock(REQUESTED)
        assert lock.acquire(blocking=False), "flash leaked the port lock"
        lock.release()
    finally:
        registry.clear_port_lock(REQUESTED)


@pytest.mark.firmware
def test_flash_start_releases_the_port_lock_when_the_job_ends(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    try:
        with patch.object(pio, "run", return_value=_Result(_WRONG_PORT_STDOUT)):
            started = flash.flash_start("meshnology_w10", REQUESTED, confirm=True)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                polled = flash.flash_poll(started["job_id"])
                if polled["status"] != "running":
                    break
                time.sleep(0.05)

        assert polled["status"] == "failed"
        lock = registry.port_lock(REQUESTED)
        assert lock.acquire(blocking=False), "the flash job leaked the port lock"
        lock.release()
    finally:
        registry.clear_port_lock(REQUESTED)


@pytest.mark.firmware
def test_erase_and_flash_holds_the_port_across_the_install_script(tmp_path) -> None:
    held: dict[str, bool] = {}

    def _stub_script(script, port, binary):
        probe = registry.port_lock(port)
        held["locked"] = not probe.acquire(blocking=False)
        if not held["locked"]:
            probe.release()
        return {"exit_code": 0, "stdout_tail": "", "stderr_tail": "", "duration_s": 0.1}

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "device-install.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    try:
        with (
            patch.object(flash, "_check_esp32_env", return_value="esp32"),
            patch.object(flash, "_factory_bin_for", return_value=tmp_path / "factory.bin"),
            patch.object(config, "firmware_root", return_value=tmp_path),
            patch.object(flash, "_run_install_script", side_effect=_stub_script),
        ):
            flash.erase_and_flash("meshnology_w10", REQUESTED, confirm=True, skip_build=True)

        assert held["locked"], "erase_and_flash ran the install script without the port lock"
        lock = registry.port_lock(REQUESTED)
        assert lock.acquire(blocking=False), "erase_and_flash leaked the port lock"
        lock.release()
    finally:
        registry.clear_port_lock(REQUESTED)
