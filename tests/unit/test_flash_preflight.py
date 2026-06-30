"""Unit tests for flash()'s pre-flight port recovery.

flash() runs the upload port through ``port_recovery.ensure_port_free`` first, so
a held/wedged device self-heals before pio uploads — and uploads to whatever
(possibly re-enumerated) path recovery returns. These pin that wiring without
touching hardware or pio.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from meshtastic_mcp import flash, pio, port_recovery


class _StubResult:
    returncode = 0
    stdout = ""
    stderr = ""
    duration_s = 0.1


def test_flash_uploads_to_recovered_port() -> None:
    """A wedged device re-enumerates on a new path; pio must upload to it."""
    captured: dict = {}

    def _stub_run(args, **kwargs):
        captured["args"] = args
        return _StubResult()

    with (
        patch.object(port_recovery, "ensure_port_free", return_value="/dev/cu.NEW") as ef,
        patch.object(pio, "run", side_effect=_stub_run),
        patch.object(flash, "_artifacts_for", return_value=[]),
    ):
        out = flash.flash("rak4631", "/dev/cu.OLD", confirm=True)

    # Pre-flighted the original port, with a power-cycle permitted.
    ef.assert_called_once()
    assert ef.call_args.args[0] == "/dev/cu.OLD"
    assert ef.call_args.kwargs.get("allow_power_cycle") is True
    # pio uploaded to the RECOVERED path, not the one we were handed.
    args = captured["args"]
    assert args[args.index("--upload-port") + 1] == "/dev/cu.NEW"
    assert out["exit_code"] == 0


def test_flash_raises_flasherror_on_recovery_failure() -> None:
    with (
        patch.object(
            port_recovery,
            "ensure_port_free",
            side_effect=port_recovery.PortRecoveryError("still wedged"),
        ),
        patch.object(pio, "run") as run,
    ):
        with pytest.raises(flash.FlashError, match="could not be made usable"):
            flash.flash("rak4631", "/dev/cu.OLD", confirm=True)
    run.assert_not_called()


def test_confirm_gate_fires_before_any_port_work() -> None:
    with patch.object(port_recovery, "ensure_port_free") as ef:
        with pytest.raises(flash.FlashError):
            flash.flash("rak4631", "/dev/cu.OLD", confirm=False)
    ef.assert_not_called()


# --- touch_1200bps / nRF52 serial-DFU bootloader detection -----------------
#
# A wedged board that lingers on the bus with a VID/PID that collides with a
# bootloader PID (LilyGO T-Echo, 0x239A/0x002A) must NOT be reported as a
# flashable serial-DFU device. `ok: true` requires a GENUINE re-enumeration:
# a brand-new port, or a PID change in place. Otherwise nrfutil DFU fails with
# "Target is not in DFU mode" while touch_1200bps claimed success.


def _dev(port: str, vid: str, pid: str) -> dict:
    return {"port": port, "vid": vid, "pid": pid, "description": None}


def _list_devices_seq(before: list, after: list):
    """list_devices stub: first call yields `before`, every later call `after`."""
    state = {"n": 0}

    def _fn(include_unknown: bool = False) -> list:
        state["n"] += 1
        return list(before if state["n"] == 1 else after)

    return _fn


def _touch(before: list, after: list, **kwargs) -> dict:
    kwargs.setdefault("poll_timeout_s", 0.05)
    kwargs.setdefault("retries", 1)
    with (
        patch.object(flash, "_do_1200bps_touch", lambda *a, **k: None),
        patch.object(flash.devices, "list_devices", side_effect=_list_devices_seq(before, after)),
        patch.object(flash.time, "sleep", lambda *a, **k: None),
    ):
        return flash.touch_1200bps("/dev/cu.echo", **kwargs)


def test_touch_rejects_wedged_app_port_with_colliding_pid() -> None:
    # Repro: T-Echo never enters DFU — same port, same (bootloader-colliding)
    # PID before and after the touch. Must report ok:false, not a phantom DFU.
    echo = _dev("/dev/cu.echo", "0x239a", "0x002a")
    res = _touch([echo], [echo])
    assert res["ok"] is False
    assert res["new_port"] is None
    assert res["new_port_vid_pid"] == (None, None)


def test_touch_accepts_new_bootloader_port() -> None:
    app = _dev("/dev/cu.echo", "0x239a", "0x002a")
    dfu = _dev("/dev/cu.dfu", "0x239a", "0x0029")  # appeared at a NEW path
    res = _touch([app], [dfu])
    assert res["ok"] is True
    assert res["new_port"] == "/dev/cu.dfu"
    assert res["new_port_vid_pid"] == ("0x239a", "0x0029")


def test_touch_accepts_in_place_pid_change_to_bootloader() -> None:
    # Same /dev path, but the PID flips app(0x8029) -> bootloader(0x0029): a
    # genuine in-place re-enumeration into serial-DFU.
    before = _dev("/dev/cu.rak", "0x239a", "0x8029")
    after = _dev("/dev/cu.rak", "0x239a", "0x0029")
    res = _touch([before], [after])
    assert res["ok"] is True
    assert res["new_port"] == "/dev/cu.rak"
    assert res["new_port_vid_pid"] == ("0x239a", "0x0029")


def test_find_bootloader_requires_new_or_changed_port() -> None:
    echo = _dev("/dev/cu.echo", "0x239a", "0x002a")
    with patch.object(flash.devices, "list_devices", return_value=[echo]):
        # Pre-touch state shows the same PID at this port -> still app mode.
        assert flash._find_nrf52_bootloader_port(before_pids={"/dev/cu.echo": 0x002A}) is None
        # Absent from the pre-touch map -> a brand-new port -> accepted.
        assert flash._find_nrf52_bootloader_port(before_pids={})["port"] == "/dev/cu.echo"
        # Back-compat: with no before-state, any matching VID/PID port qualifies.
        assert flash._find_nrf52_bootloader_port()["port"] == "/dev/cu.echo"


# --- silent nRF52 DFU upload-failure detection -----------------------------
#
# `pio run -t upload` exits 0 even when adafruit-nrfutil's serial DFU actually
# failed ("No data received / Target is not in DFU mode"). flash() must surface
# that as a non-zero exit, or the bake/flash/recover paths record a phantom
# flash and mark an unprovisioned board as baked.


class _DfuFailResult:
    # What a silently-failed nRF52 DFU upload looks like: pio exits 0, but
    # adafruit-nrfutil printed the failure. This is the real wedged-T-Echo case.
    returncode = 0
    stdout = (
        "Forcing reset using 1200bps open/close on port /dev/cu.usbmodem143101\n"
        "Uploading .pio/build/t-echo-plus/firmware.zip\n"
        "Failed to upgrade target. Error is: No data received on serial port. "
        "Not able to proceed.\n"
        "Possible causes:\n- Target is not in DFU mode.\n"
        "========================= [SUCCESS] Took 32.50 seconds ===============\n"
    )
    stderr = "Timed out waiting for acknowledgement from device."
    duration_s = 32.5


def test_flash_fails_on_silent_dfu_failure() -> None:
    """pio exits 0 but nrfutil's DFU upload actually failed — flash() must NOT
    report success, or the bake/flash/recover paths record a phantom flash and
    mark an unprovisioned board as baked."""
    with (
        patch.object(port_recovery, "ensure_port_free", return_value="/dev/cu.usbmodem143101"),
        patch.object(pio, "run", return_value=_DfuFailResult()),
    ):
        out = flash.flash("t-echo-plus", "/dev/cu.usbmodem143101", confirm=True)

    assert out["exit_code"] != 0, "silent DFU failure must not look like success"
    assert out["upload_error"] == "Failed to upgrade target"


def test_flash_clean_upload_still_succeeds() -> None:
    """A normal upload (no failure markers) stays exit 0 with no upload_error —
    the detector must not false-positive on healthy output."""

    class _OK:
        returncode = 0
        stdout = "Uploading...\n[SUCCESS] Took 20s\n"
        stderr = ""
        duration_s = 20.0

    with (
        patch.object(port_recovery, "ensure_port_free", return_value="/dev/cu.X"),
        patch.object(pio, "run", return_value=_OK()),
    ):
        out = flash.flash("t-echo-plus", "/dev/cu.X", confirm=True)

    assert out["exit_code"] == 0
    assert "upload_error" not in out


def test_detect_upload_failure_unit() -> None:
    assert flash._detect_upload_failure("all good", "") is None
    assert flash._detect_upload_failure("", "Target is not in DFU mode") == (
        "Target is not in DFU mode"
    )
    assert flash._detect_upload_failure(None, None) is None
