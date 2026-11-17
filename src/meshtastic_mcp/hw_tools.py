# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Direct wrappers around vendor flashing tools: esptool, nrfutil, picotool.

These are escape hatches. Prefer the pio-based tools in flash.py when they
cover the operation — pio knows the correct offsets, protocols, and filters
for every supported board. Use these when pio doesn't: to erase a bricked
ESP32, DFU-flash an nRF52 zip package, or inspect an RP2040's bootloader.

Every destructive `*_raw` subcommand is gated by `confirm=True` so callers
can't accidentally `--write-flash` from freeform args.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import config, connection, pio

_TIMEOUT_SHORT = 30
_TIMEOUT_LONG = 600


class ToolError(RuntimeError):
    pass


def _destructive_word(args: Sequence[str], destructive: set[str]) -> str | None:
    """The first non-flag argument that names a destructive subcommand, wherever it sits.

    Flags take values (`--port /dev/x`, `-b 921600`), so the first non-flag argument is not
    always the subcommand; checking every one means a value cannot hide `erase_flash` from the
    gate. A value that happens to equal a subcommand name asks for confirmation it did not need.
    """
    wanted = {s.replace("-", "_") for s in destructive}
    return next((a for a in args if not a.startswith("-") and a.replace("-", "_") in wanted), None)


class ConfirmRequired(ToolError):
    """A destructive call was made without `confirm=True`; raised before any side effect."""

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} is destructive and requires confirm=True.")
        self.what = what


def _run(
    binary: Path,
    args: Sequence[str],
    *,
    timeout: float = _TIMEOUT_LONG,
    cwd: Path | None = None,
) -> dict[str, Any]:
    # Shared with pio.run(): if `MESHTASTIC_MCP_FLASH_LOG` is set, each line
    # of output is tee'd to that file as it arrives so the TUI can show live
    # esptool/nrfutil/picotool progress instead of 3 minutes of silence.
    full = [str(binary), *args]
    try:
        rc, stdout, stderr, duration = pio._run_capturing(
            full,
            cwd=cwd,
            timeout=timeout,
            tee_header=f"{binary.name} {' '.join(args)}",
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{binary.name} {' '.join(args)} timed out after {timeout}s") from exc
    return {
        "exit_code": rc,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_tail": pio.tail_lines(stdout, 200),
        "stderr_tail": pio.tail_lines(stderr, 200),
        "duration_s": round(duration, 2),
    }


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise ConfirmRequired(what)


# ---------- esptool --------------------------------------------------------

ESPTOOL_DESTRUCTIVE = {
    "write_flash",
    "write-flash",
    "erase_flash",
    "erase-flash",
    "erase_region",
    "erase-region",
    "merge_bin",
    "merge-bin",
}


def _parse_esptool_chip_info(stdout: str) -> dict[str, Any]:
    """Parse `esptool chip_id` / `flash_id` output into structured fields."""
    result: dict[str, Any] = {
        "chip": None,
        "mac": None,
        "crystal_mhz": None,
        "flash_size": None,
        "features": [],
    }
    for line in stdout.splitlines():
        line = line.strip()
        if m := re.match(r"Chip is (.+)", line):
            result["chip"] = m.group(1).strip()
        elif m := re.match(r"MAC: ([0-9a-fA-F:]+)", line):
            result["mac"] = m.group(1)
        elif m := re.match(r"Crystal is (\d+)MHz", line):
            result["crystal_mhz"] = int(m.group(1))
        elif m := re.match(r"Detected flash size: (\S+)", line):
            result["flash_size"] = m.group(1)
        elif m := re.match(r"Features: (.+)", line):
            result["features"] = [f.strip() for f in m.group(1).split(",") if f.strip()]
    return result


def esptool_chip_info(port: str) -> dict[str, Any]:
    connection.reject_if_tcp(port, "esptool_chip_info")
    binary = config.esptool_bin()
    # `chip_id` prints chip + mac + crystal + features. `flash_id` adds flash.
    combined = _run(binary, ["--port", port, "flash_id"], timeout=_TIMEOUT_SHORT)
    if combined["exit_code"] != 0:
        raise ToolError(
            f"esptool failed (exit {combined['exit_code']}):\n{combined['stderr_tail']}"
        )
    parsed = _parse_esptool_chip_info(combined["stdout"])
    return {**parsed, "raw_stdout_tail": combined["stdout_tail"]}


def esptool_erase_flash(port: str, confirm: bool = False) -> dict[str, Any]:
    """Full-chip erase. Leaves the device unbootable until reflashed."""
    _require_confirm(confirm, "esptool_erase_flash")
    connection.reject_if_tcp(port, "esptool_erase_flash")
    binary = config.esptool_bin()
    # esptool v5 uses `erase-flash`, older uses `erase_flash`. Try the new name
    # first; if it fails with unknown command, retry old.
    res = _run(binary, ["--port", port, "erase-flash"], timeout=_TIMEOUT_LONG)
    if res["exit_code"] != 0 and "unrecognized" in (res["stderr"] or res["stdout"]).lower():
        res = _run(binary, ["--port", port, "erase_flash"], timeout=_TIMEOUT_LONG)
    return res


def esptool_raw(args: list[str], port: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Raw esptool passthrough. Destructive subcommands require confirm=True."""
    if not args:
        raise ToolError("args must not be empty")
    connection.reject_if_tcp(port, "esptool_raw")
    subcommand = _destructive_word(args, ESPTOOL_DESTRUCTIVE)
    if subcommand:
        _require_confirm(confirm, f"esptool {subcommand}")

    binary = config.esptool_bin()
    full_args: list[str] = []
    if port:
        full_args.extend(["--port", port])
    full_args.extend(args)
    return _run(binary, full_args, timeout=_TIMEOUT_LONG)


# ---------- nrfutil --------------------------------------------------------

NRFUTIL_DESTRUCTIVE = {"dfu", "settings"}


_BY_ID = Path("/dev/serial/by-id")
_DFU_ENTRY_WAIT_S = 8.0


def _by_id_names(device: str) -> set[str]:
    try:
        return {p.name for p in _BY_ID.iterdir() if os.path.realpath(p) == device}
    except OSError:
        return set()


def touch_1200(
    port: str, *, settle_s: float = 0.1, timeout_s: float = 3.0, drop_dtr: bool = True
) -> None:
    """The 1200 baud USB touch that resets a board into its bootloader, bounded by a worker.

    The Adafruit nRF52 core enters DFU on a DTR-low line state while the line coding is 1200
    baud. A plain close lowers DTR only when the tty has HUPCL set, and meshtastic-python clears
    HUPCL on every POSIX open, so `drop_dtr` lowers it explicitly, as PlatformIO's touch does.
    ESP32-S3 native USB resets on the 1200 baud line coding alone.

    The open and the close can both block on a busy CDC device, so the caller returns within
    `timeout_s` regardless; a worker still blocked in the close has already sent the signal.
    A port that does not exist raises `serial.SerialException`; other errors during the open
    are expected while the board resets and are ignored.
    """
    import serial

    errors: list[BaseException] = []

    def _inner() -> None:
        try:
            s = serial.Serial(port, 1200)
        except serial.SerialException as exc:
            if "No such file" in str(exc) or "could not open" in str(exc).lower():
                raise
            return
        try:
            if drop_dtr:
                # A line left DTR-low by an earlier session resets the board on the 1200 baud
                # open, so the ioctl can meet a device that is already gone: the touch worked.
                try:
                    s.dtr = False
                except (OSError, serial.SerialException):
                    return
            time.sleep(settle_s)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _runner() -> None:
        try:
            _inner()
        except BaseException as exc:  # re-raised on the caller's thread after the join
            errors.append(exc)

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout=timeout_s)
    if worker.is_alive():
        return
    if errors:
        raise errors[0]


def _touch_into_dfu(device: str) -> None:
    """Touch an nRF52 into serial DFU and wait for its bootloader to appear.

    The bootloader shows up on the same kernel node under a new by-id name; if no new name
    appears the board may already be in DFU, and the transfer decides.
    """
    import serial

    before = _by_id_names(device)
    try:
        touch_1200(device, drop_dtr=True)
    except serial.SerialException as exc:
        raise ToolError(f"could not touch {device} into DFU: {exc}") from exc
    deadline = time.monotonic() + _DFU_ENTRY_WAIT_S
    while time.monotonic() < deadline:
        names = _by_id_names(device)
        if names and names != before:
            time.sleep(0.5)
            return
        time.sleep(0.2)


def nrfutil_dfu(port: str, package_path: str, confirm: bool = False) -> dict[str, Any]:
    _require_confirm(confirm, "nrfutil_dfu")
    connection.reject_if_tcp(port, "nrfutil_dfu")
    pkg = Path(package_path).expanduser()
    if not pkg.is_file():
        raise ToolError(f"Package not found: {pkg}")
    binary = config.nrfutil_bin()
    adafruit = "adafruit" in binary.name
    # The bootloader re-enumerates under a different /dev/serial/by-id name on the same kernel
    # node, so a by-id path stops existing between the touch and the transfer.
    device = os.path.realpath(port) if adafruit else port
    args = ["dfu", "serial", "--package", str(pkg), "--port", device, "-b", "115200"]
    if adafruit:
        # Single bank matches PlatformIO's nRF52 upload. The bootloader leaves DFU ~30 s after
        # a touch, so the touch happens here, right before the transfer.
        args.append("--singlebank")
        if _BY_ID.is_dir():
            _touch_into_dfu(device)
        else:
            args += ["-t", "1200"]
    res = _run(binary, args, timeout=_TIMEOUT_LONG)
    # adafruit-nrfutil prints "Failed to upgrade target" and still exits 0; only a run that
    # printed "Device programmed." wrote the image.
    if adafruit and res["exit_code"] == 0 and "Device programmed." not in res["stdout"]:
        res["exit_code"] = 1
    return res


def nrfutil_raw(args: list[str], confirm: bool = False) -> dict[str, Any]:
    if not args:
        raise ToolError("args must not be empty")
    subcommand = _destructive_word(args, NRFUTIL_DESTRUCTIVE)
    if subcommand:
        _require_confirm(confirm, f"nrfutil {subcommand}")
    binary = config.nrfutil_bin()
    return _run(binary, args, timeout=_TIMEOUT_LONG)


# ---------- picotool -------------------------------------------------------

PICOTOOL_DESTRUCTIVE = {"load", "reboot", "save", "erase"}


def _parse_picotool_info(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "vendor": None,
        "product": None,
        "serial": None,
        "flash_size": None,
        "program_name": None,
        "program_version": None,
    }
    for line in stdout.splitlines():
        line = line.strip()
        if m := re.match(r"Program information:", line):
            continue
        if m := re.match(r"name:\s*(.+)", line):
            result["program_name"] = m.group(1).strip()
        elif m := re.match(r"version:\s*(.+)", line):
            result["program_version"] = m.group(1).strip()
        elif m := re.match(r"vendor:\s*(.+)", line):
            result["vendor"] = m.group(1).strip()
        elif m := re.match(r"product:\s*(.+)", line):
            result["product"] = m.group(1).strip()
        elif m := re.match(r"serial number:\s*(.+)", line):
            result["serial"] = m.group(1).strip()
        elif m := re.match(r"flash size:\s*(.+)", line):
            result["flash_size"] = m.group(1).strip()
    return result


def picotool_info(port: str | None = None) -> dict[str, Any]:
    """Read device info from a Pico in BOOTSEL mode. `port` is informational
    only — picotool auto-detects."""
    connection.reject_if_tcp(port, "picotool_info")
    binary = config.picotool_bin()
    res = _run(binary, ["info", "-a"], timeout=_TIMEOUT_SHORT)
    if res["exit_code"] != 0:
        raise ToolError(
            f"picotool info failed (exit {res['exit_code']}): "
            "is the Pico in BOOTSEL mode?\n" + res["stderr_tail"]
        )
    parsed = _parse_picotool_info(res["stdout"])
    return {**parsed, "raw_stdout_tail": res["stdout_tail"]}


def picotool_load(uf2_path: str, confirm: bool = False) -> dict[str, Any]:
    _require_confirm(confirm, "picotool_load")
    uf2 = Path(uf2_path).expanduser()
    if not uf2.is_file():
        raise ToolError(f"UF2 not found: {uf2}")
    binary = config.picotool_bin()
    return _run(binary, ["load", "-x", "-t", "uf2", str(uf2)], timeout=_TIMEOUT_LONG)


def picotool_raw(args: list[str], confirm: bool = False) -> dict[str, Any]:
    if not args:
        raise ToolError("args must not be empty")
    subcommand = _destructive_word(args, PICOTOOL_DESTRUCTIVE)
    if subcommand:
        _require_confirm(confirm, f"picotool {subcommand}")
    binary = config.picotool_bin()
    return _run(binary, args, timeout=_TIMEOUT_LONG)
