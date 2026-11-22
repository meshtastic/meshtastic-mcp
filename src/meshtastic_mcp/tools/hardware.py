# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Hardware tools: serial sessions, USB power, vendor flash tools, nRF52 UF2 and the webcam."""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, cast

from fastmcp import Context
from fastmcp.exceptions import ToolError

from .. import (
    backends,
    config,
    connection,
    flash,
    hw_tools,
    nrf52_bootloader,
    registry,
    serial_session,
    uf2_host,
)
from .. import confirm as confirm_mod
from .. import tasks as tasks_mod
from ..schemas import InstallResult, SerialCloseResult, VendorResult
from ..tool_meta import meta
from ._app import app
from ._shared import _confirmed_hw_call, _off_loop, _run_job


@app.tool()
@meta("destructive", open_world=True)
def touch_1200bps(port: str, settle_ms: int = 250) -> dict[str, Any]:
    """Open `port` at 1200 baud and close it, rebooting an nRF52840, native-USB ESP32-S3,
    RP2040 or similar into its USB bootloader; on an nRF52 it also drops DTR, which its
    Adafruit core needs.

    Then reports the bootloader's port: a new one, or the same node under a new PID (Linux
    keeps the /dev/ttyACM node and renames the /dev/serial/by-id link). Erases nothing; it
    is annotated destructive because it reboots the device.

    Returns `{ok, former_port, new_port, new_port_vid_pid, ...}`.

    Args:
        port: The board's serial port.
        settle_ms: Milliseconds to hold the port open at 1200 baud before closing it.
    """
    return flash.touch_1200bps(port, settle_ms=settle_ms)


# ---------- Serial log sessions -------------------------------------------


@app.tool()
@meta("destructive", open_world=True)
def serial_open(
    port: str,
    baud: int = 115200,
    env: str | None = None,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Open a `pio device monitor` session reading from `port`.

    Needs PlatformIO and `MESHTASTIC_FIRMWARE_ROOT`. The session holds the port until
    `serial_close`, so the admin tools cannot use it meanwhile. Returns `{session_id,
    resolved_baud, resolved_filters, env}`: the `session_id` for `serial_close`, plus the
    baud and filters pio resolved. The output lands in the recorder for `query`.

    Args:
        port: The serial port to read.
        baud: Line speed, used without `env`.
        env: A PlatformIO env whose `monitor_speed` and `monitor_filters` pio takes from
            platformio.ini, which turns on the ESP32 exception decoders for firmware
            debugging.
        filters: Monitor filters, used without `env`: default ["direct"]; others are time,
            hexlify, esp32_exception_decoder, esp32_c3_exception_decoder, log2file.
    """
    # Enforce the exclusive-port invariant atomically. The admin path
    # (connection.connect) already checks active_session_for_port + holds
    # port_lock; mirror that here so two concurrent serial_open calls (or a
    # serial_open racing an admin connect) can't both own `port`. The lock is
    # held only across the check + spawn + register; the running monitor's
    # ongoing ownership is then tracked via the registry session.
    lock = registry.port_lock(port)
    if not lock.acquire(blocking=False):
        raise connection.ConnectionError(
            f"Port {port} is busy — another device operation is in flight. Retry shortly."
        )
    try:
        active = registry.active_session_for_port(port)
        if active is not None:
            raise connection.ConnectionError(
                f"Port {port} is held by serial session {active.id}. Call `serial_close` first."
            )
        session = serial_session.open_session(port=port, baud=baud, env=env, filters=filters)
        registry.register_session(session)
    finally:
        lock.release()
    return {
        "session_id": session.id,
        "resolved_baud": session.baud,
        "resolved_filters": session.filters,
        "env": session.env,
    }


@app.tool()
@meta("read", open_world=True)
def serial_read(
    session_id: str,
    max_lines: int = 200,
    since_cursor: int | None = None,
) -> dict[str, Any]:
    """Read buffered lines from a serial monitor session.

    Returns `{lines, new_cursor, eof, dropped}`. `dropped` counts lines that aged out of
    the 10k-line ring buffer between reads, so a value > 0 means you missed data.

    Args:
        session_id: Id from `serial_open`.
        max_lines: Cap on the lines returned.
        since_cursor: Omit to get everything since your last `serial_read` (an advancing
            cursor). Pass `since_cursor=N` to re-read from a specific point, or
            `since_cursor=0` to read from the start of the in-memory buffer.
    """
    session = registry.get_session(session_id)
    return serial_session.read_session(session, max_lines=max_lines, since_cursor=since_cursor)


@app.tool()
@meta("read")
def serial_list() -> list[dict[str, Any]]:
    """List all active serial monitor sessions."""
    return [serial_session.session_summary(s) for s in registry.all_sessions()]


@app.tool()
@meta("destructive")
def serial_close(session_id: str) -> SerialCloseResult:
    """Terminate a serial monitor session and free its port.

    Returns `{ok: true}` on success, or `{ok: false, reason}` if the session is unknown.

    Args:
        session_id: Id from `serial_open`.
    """
    session = registry.remove_session(session_id)
    if session is None:
        return {"ok": False, "reason": f"Unknown session_id {session_id!r}"}
    serial_session.close_session(session)
    return {"ok": True}


@app.tool()
@meta("read", open_world=True)
def capture_screen(role: str | None = None, ocr: bool = True) -> dict[str, Any]:
    """Grab a frame from the USB webcam pointed at the device screen.

    Returns `{backend, bytes, image_base64, ocr_backend?, ocr_text?}`: PNG bytes (base64),
    optional OCR text, and backend metadata. Requires the `[ui]` extras
    (opencv-python-headless) and a camera configured via
    `MESHTASTIC_UI_CAMERA_DEVICE[_<ROLE>]`. Falls back to a 1×1 black PNG from the null
    backend when no camera is configured. The screen can show mesh-sourced text.

    Args:
        role: Which camera, picking `MESHTASTIC_UI_CAMERA_DEVICE_<ROLE>`; omit for the
            default one.
        ocr: Also run OCR on the frame.
    """
    import base64

    from .. import camera as camera_mod

    cam = camera_mod.get_camera(role)
    try:
        png = cam.capture()
    finally:
        cam.close()

    result: dict[str, Any] = {
        "backend": cam.name,
        "bytes": len(png),
        "image_base64": base64.b64encode(png).decode("ascii"),
    }
    if ocr:
        from .. import ocr as ocr_mod

        result["ocr_backend"] = ocr_mod.backend_name()
        result["ocr_text"] = ocr_mod.ocr_text(png)
    return result


# ---------- USB power control (uhubctl) -----------------------------------


@app.tool()
@meta("read", open_world=True, title="USB Hub List")
def uhubctl_list() -> list[dict[str, Any]]:
    """List every USB hub + per-port device attachment as seen by `uhubctl`. Read-only.

    Each hub has its location, descriptor, whether it supports per-port power switching,
    and its populated ports with the attached VID:PID; check it before a power-cycle.
    `location` is uhubctl notation (`1-1.3`) on Linux and macOS. On Windows the backend is
    VirtualHere's `vhfilter` (uhubctl cannot switch port power there) and a location is
    the hub's PnP device path, e.g. `USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2`. Pass either
    form to the power tools unchanged.
    """
    from .. import uhubctl as uhubctl_mod

    return uhubctl_mod.list_hubs()


@app.tool()
@meta("destructive", open_world=True, title="USB Hub Port Power", port="not_device")
async def uhubctl_power(
    ctx: Context,
    action: str,
    location: str | None = None,
    port: int | None = None,
    role: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power a USB hub port on or off (`uhubctl -a on|off`, or `vhfilter
    --switch-port` on Windows).

    Target the port by either (`location`, `port`), e.g. `location="1-1.3", port=2` as
    `uhubctl_list` reports it, OR by `role`, never both. `action="off"` requires
    `confirm=True`, or the user's answer when the client can ask: the attached device
    disappears from the OS at once. `on` never asks.

    Returns `{action, location, port, delay_s, duration_s, ...}`.

    Args:
        action: `on` or `off`.
        location: The hub location from `uhubctl_list`: uhubctl notation, or a PnP device
            path on Windows.
        port: The port number on that hub.
        role: A device role ("nrf52", "esp32s3"). Role lookup honors
            `MESHTASTIC_UHUBCTL_LOCATION_<ROLE>` + `_PORT_<ROLE>` env vars first, then
            falls back to VID auto-detection.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human. Only `off` asks.
    """
    from .. import uhubctl as uhubctl_mod

    action_lower = action.lower()
    if action_lower not in {"on", "off"}:
        raise ValueError(f"action must be 'on' or 'off', got {action!r}")
    loc, p = _resolve_uhubctl_target(location, port, role)
    if action_lower == "off":
        pending = await confirm_mod.gate(ctx, f"cut power to the USB hub port {loc}:{p}", confirm)
        if pending is not None:
            return cast(dict[str, Any], pending)
    if action_lower == "on":
        return cast(dict[str, Any], await _off_loop(lambda: uhubctl_mod.power_on(loc, p)))
    return cast(dict[str, Any], await _off_loop(lambda: uhubctl_mod.power_off(loc, p)))


@app.tool()
@meta("destructive", open_world=True, title="USB Hub Port Cycle", port="not_device")
async def uhubctl_cycle(
    ctx: Context,
    location: str | None = None,
    port: int | None = None,
    role: str | None = None,
    delay_s: int = 2,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power a USB hub port off, wait `delay_s` seconds, then on.

    The hard reset for a board whose USB serial has wedged (calls to it fail with
    "Power-cycle it (uhubctl_cycle)"). One call instead of off+on, with the timing handled
    in-process. Target by (location, port) from `uhubctl_list`, or by role (see
    `uhubctl_power`). Requires `confirm=True`, or the user's answer when the client can
    ask.

    Returns `{action, location, port, delay_s, duration_s, ...}`.

    Args:
        location: The hub location from `uhubctl_list`.
        port: The port number on that hub.
        role: A device role, resolved as in `uhubctl_power`; not with `location`/`port`.
        delay_s: Seconds the port stays off, 0..60.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    from .. import uhubctl as uhubctl_mod

    if delay_s < 0 or delay_s > 60:
        raise ValueError(f"delay_s must be 0..60, got {delay_s}")
    loc, p = _resolve_uhubctl_target(location, port, role)
    pending = await confirm_mod.gate(ctx, f"power-cycle the USB hub port {loc}:{p}", confirm)
    if pending is not None:
        return cast(dict[str, Any], pending)
    return cast(dict[str, Any], await _off_loop(lambda: uhubctl_mod.cycle(loc, p, delay_s=delay_s)))


def _resolve_uhubctl_target(
    location: str | None, port: int | None, role: str | None
) -> tuple[str, int]:
    """Shared arg-resolution for uhubctl_power + uhubctl_cycle."""
    from .. import uhubctl as uhubctl_mod

    if role is not None:
        if location is not None or port is not None:
            raise ValueError("pass either `role` OR (`location` + `port`), not both")
        return uhubctl_mod.resolve_target(role)
    if location is None or port is None:
        raise ValueError("must pass `role` or both `location` and `port`")
    return (location, int(port))


# ---------- Direct hardware tools -----------------------------------------


@app.tool()
@meta("read", open_world=True, title="ESPTool Chip Info")
def esptool_chip_info(port: str) -> dict[str, Any]:
    """Run `esptool flash_id` and return chip, MAC, crystal, and flash size.

    Read-only, so no confirm is required. Prefer this over parsing pio upload logs when you
    just want to identify the chip.

    Args:
        port: The ESP32's serial port.
    """
    return hw_tools.esptool_chip_info(port)


@app.tool()
@meta("destructive", open_world=True, title="ESPTool Erase Flash")
async def esptool_erase_flash(ctx: Context, port: str, confirm: bool = False) -> dict[str, Any]:
    """Full-chip erase via `esptool erase_flash`. Leaves the device unbootable.

    Prefer `erase_and_flash` which also writes firmware. Use this only for recovery when a
    device is in a weird state. Requires confirm=True, or the user's answer when the client
    can ask (always, with `MESHTASTIC_MCP_CONFIRM=human`).

    Args:
        port: The ESP32's serial port.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    return await _confirmed_hw_call(
        ctx, lambda c: hw_tools.esptool_erase_flash(port, confirm=c), confirm
    )


@app.tool()
@meta("destructive", open_world=True, title="ESPTool Raw Pass-Through", port="finds_own")
async def esptool_raw(
    ctx: Context, args: list[str], port: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Pass-through to `esptool`.

    Read-only subcommands (chip_id, flash_id, read_mac, read_flash_status, get_security_info,
    image_info, version) run at once; any other needs confirm=True, or the user's answer when
    the client can ask. Prefer the high-level `pio_flash` / `erase_and_flash` / `update_flash`
    tools where possible; they know board-specific offsets and protocols.

    Security: `args` is passed directly to the esptool binary. All arguments must come from
    a trusted source; do not allow untrusted content (e.g. mesh packet payloads) to flow
    into argument lists.

    Returns `{exit_code, stdout, stderr, stdout_tail, stderr_tail, duration_s}`.

    Args:
        args: The esptool arguments, from a trusted source only.
        port: Serial port, passed as `--port`; omit to leave it to `args` or esptool.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human. Only subcommands that are not read-only ask.
    """
    return await _confirmed_hw_call(
        ctx, lambda c: hw_tools.esptool_raw(args, port=port, confirm=c), confirm
    )


@app.tool()
@meta("destructive", open_world=True, title="nRF DFU Flash")
async def nrfutil_dfu(
    ctx: Context, port: str, package_path: str, confirm: bool = False
) -> dict[str, Any]:
    """DFU-flash a .zip package to an nRF52840 via `nrfutil dfu serial`.

    With adafruit-nrfutil the transfer uses `--singlebank` and fails unless the run printed
    "Device programmed."; where `/dev/serial/by-id` exists the tool does the 1200 baud
    touch itself (dropping DTR) and waits for the bootloader, elsewhere it passes
    `-t 1200`. For a board already sitting in serial DFU, use `vendor(tool="nrfutil")` with no touch
    instead. On the Meshtastic bootloader `nrf52_flash_uf2` is several times faster; for
    firmware built from the tree prefer `pio_flash`. Requires confirm=True, or the user's
    answer when the client can ask (always, with `MESHTASTIC_MCP_CONFIRM=human`).

    Args:
        port: The app's serial port; a `/dev/serial/by-id` link works.
        package_path: The DFU .zip package.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    return await _confirmed_hw_call(
        ctx, lambda c: hw_tools.nrfutil_dfu(port, package_path, confirm=c), confirm
    )


@app.tool()
@meta("destructive", open_world=True, title="nRFUtil Raw Pass-Through", port="finds_own")
async def nrfutil_raw(ctx: Context, args: list[str], confirm: bool = False) -> dict[str, Any]:
    """Pass-through to `nrfutil`.

    Only `version`, `list`, `device list`, `device device-info` and `device core-info` run
    at once; anything else needs confirm=True, or the user's answer when the client can ask.
    Recovers an nRF52 left sitting in serial DFU, with no touch, as
    `args=["dfu", "serial", "--package", "<zip>", "--port", "/dev/ttyACM0",
    "-b", "115200", "--singlebank"]` (adafruit-nrfutil).

    Security: `args` is passed directly to the nrfutil binary. All arguments must come from
    a trusted source; do not allow untrusted content to flow into argument lists.

    Returns `{exit_code, stdout, stderr, stdout_tail, stderr_tail, duration_s}`.

    Args:
        args: The nrfutil arguments, from a trusted source only.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human. Only calls outside the read-only list ask.
    """
    return await _confirmed_hw_call(ctx, lambda c: hw_tools.nrfutil_raw(args, confirm=c), confirm)


_VENDORS = {
    "esptool": lambda args, port, c: hw_tools.esptool_raw(args, port=port, confirm=c),
    "nrfutil": lambda args, port, c: hw_tools.nrfutil_raw(args, confirm=c),
    "picotool": lambda args, port, c: hw_tools.picotool_raw(args, confirm=c),
}


@app.tool()
@meta("destructive", title="Vendor Flash Tool Pass-Through", port="finds_own")
async def vendor(
    ctx: Context,
    tool: Literal["esptool", "nrfutil", "picotool"],
    args: list[str],
    port: str | None = None,
    confirm: bool = False,
) -> VendorResult:
    """Run esptool, nrfutil or picotool with arguments, for what `install` and `admin` do not do.

    A read-only subcommand (esptool `flash_id`, nrfutil `device list`, picotool `info` and the
    like) runs at once. Any other needs `confirm=True` or the user's answer, and counts as
    irreversible. A failed command is a tool error carrying both
    output tails. Prefer `install` to flash firmware and `admin` for radio actions.

    Returns `{exit_code, stdout_tail, stderr_tail, duration_s, ...}`.

    Args:
        tool: `esptool`, `nrfutil` (adafruit-nrfutil or Nordic nRF Util) or `picotool`.
        args: The arguments after the tool name, such as `["chip_id"]` or
            `["dfu", "serial", "--package", "fw.zip", "--port", "/dev/ttyACM0"]`.
        port: For esptool, the serial port (`--port`); the others take it in `args`.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    run = _VENDORS[tool]
    return cast(VendorResult, await _confirmed_hw_call(ctx, lambda c: run(args, port, c), confirm))


@app.tool()
@meta("read", open_world=True)
def picotool_info(port: str | None = None) -> dict[str, Any]:
    """Run `picotool info -a`.

    Requires the RP2040 to be in BOOTSEL mode (hold BOOTSEL button while plugging in, or
    call `touch_1200bps` if the firmware supports 1200bps-reset).

    Args:
        port: Informational only; picotool finds the device itself.
    """
    return hw_tools.picotool_info(port=port)


@app.tool()
@meta("destructive", open_world=True, title="Picotool Load UF2", port="finds_own")
async def picotool_load(ctx: Context, uf2_path: str, confirm: bool = False) -> dict[str, Any]:
    """Load a UF2 to a Pico in BOOTSEL mode via `picotool load -x -t uf2`.

    Prefer `pio_flash` for flashing firmware built from this repo. Requires confirm=True,
    or the user's answer when the client can ask.

    Args:
        uf2_path: The .uf2 file to load.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    return await _confirmed_hw_call(
        ctx, lambda c: hw_tools.picotool_load(uf2_path, confirm=c), confirm
    )


@app.tool()
@meta("destructive", open_world=True, title="Picotool Raw Pass-Through", port="finds_own")
async def picotool_raw(ctx: Context, args: list[str], confirm: bool = False) -> dict[str, Any]:
    """Pass-through to `picotool`.

    Only `info`, `version` and `help` run at once; anything else needs confirm=True, or the
    user's answer when the client can ask.

    Security: `args` is passed directly to the picotool binary. All arguments must come
    from a trusted source; do not allow untrusted content to flow into argument lists.

    Returns `{exit_code, stdout, stderr, stdout_tail, stderr_tail, duration_s}`.

    Args:
        args: The picotool arguments, from a trusted source only.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human. Only calls outside info, version and help ask.
    """
    return await _confirmed_hw_call(ctx, lambda c: hw_tools.picotool_raw(args, confirm=c), confirm)


# ---------- nRF52 bootloader UF2 drive --------------------------------------
# The Meshtastic nRF52 bootloader (meshtastic/nRF52_Bootloader) shows a USB drive in UF2 mode:
# INFO_UF2.TXT names its version, board and SoftDevice and whether it takes the factory-erase
# file, and CURRENT.UF2 is the installed app. See nrf52_bootloader.py.

# Application start for each SoftDevice; a UF2 built for the other one does not boot.
_SD_APP_START = {"S140 6.1.1": 0x26000, "S140 7.3.0": 0x27000}
_BY_ID_SERIAL = re.compile(r"_([0-9A-Fa-f]{8,})-if\d+$")


def _uf2_drive(port: str | None) -> dict[str, Any]:
    """The mounted UF2 drive for `port`'s board, or the only one mounted when `port` is None."""
    drives = nrf52_bootloader.find_uf2_drives().get("drives", [])
    if port is None:
        if len(drives) == 1:
            return drives[0]
        if not drives:
            raise ToolError(
                "no UF2 drive is mounted; put the board in UF2 mode with nrf52_enter_uf2, and "
                "mount its drive on a host that does not automount"
            )
        raise ToolError(
            f"{len(drives)} UF2 drives are mounted; pass the board's port "
            "(nrf52_bootloader_info with no port lists them)"
        )
    # In UF2 mode the app's by-id link is gone, but its name still carries the USB serial the
    # drive reports.
    serial = None
    if not os.path.exists(port):
        m = _BY_ID_SERIAL.search(os.path.basename(port))
        serial = m.group(1) if m else None
    match = nrf52_bootloader.find_drive_for_port(
        port=None if serial else port, serial_number=serial, drives=drives
    )
    if not match.get("ok"):
        reason = match.get("error") or f"no mounted UF2 drive belongs to {port}"
        raise ToolError(
            f"{reason}. Check the board is in UF2 mode with its drive mounted, and pass the app "
            "port it had before (its /dev/serial/by-id path still identifies it)"
        )
    return cast(dict[str, Any], match["drive"])


def _enter_uf2(port: str | None) -> None:
    """Ask the app to reboot into the bootloader's UF2 drive (`enter_dfu_mode_request`)."""
    try:
        backends.for_target(port).admin(port, "enter_uf2", None, 0, False, 10.0)
    except (backends.BackendError, connection.ConnectionError) as exc:
        raise ToolError(str(exc)) from exc


def _wait_for_uf2_drive(port: str, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return _uf2_drive(port)
        except ToolError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.5)


@app.tool()
@meta("read", open_world=True, title="nRF52 Bootloader Info")
def nrf52_bootloader_info(port: str | None = None) -> dict[str, Any]:
    """Read the mounted nRF52 bootloader UF2 drive(s): bootloader version, board, SoftDevice,
    whether it accepts the factory-erase file, and any recorded fault (`Last-Fault`).

    The board must already be in UF2 mode: `nrf52_enter_uf2`, or double-press reset.
    Returns `{drive}` for one board, or `{ok, count, drives, searched_roots, ...}`.

    Args:
        port: Returns the drive for that board, matched by USB serial: pass the app port you
            used before, ideally its `/dev/serial/by-id` path, which still works after the
            link is gone in UF2 mode. Without it, lists every mounted UF2 drive.
    """
    if port is not None:
        return {"drive": _uf2_drive(port)}
    return nrf52_bootloader.find_uf2_drives()


@app.tool()
@meta("destructive", open_world=True, title="nRF52 Enter UF2 Mode")
async def nrf52_enter_uf2(
    ctx: Context, port: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Reboot an nRF52 node into its bootloader's UF2 drive and return the drive's info.

    Sends `enter_dfu_mode_request` over the phone API, so the app must be running; for a hung
    app, double-press reset instead. The drive appears within a couple of seconds. The node
    stays in UF2 mode until a UF2 is written to it or it is power-cycled (`uhubctl_cycle`).
    Requires confirm=True, or the user's answer when the client can ask.

    Returns `{ok, requested, drive}`.

    Args:
        port: The serial port of a board attached to this host by USB (the UF2 drive mounts
            here, so `tcp://` cannot work); omit to auto-select the one likely Meshtastic
            device. Keep passing the same `port` to the other `nrf52_*` tools.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    pending = await confirm_mod.gate(
        ctx, f"reboot the node on {port or 'the default port'} into its UF2 bootloader", confirm
    )
    if pending is not None:
        return cast(dict[str, Any], pending)
    resolved = connection.resolve_port(port)

    def _enter() -> dict[str, Any]:
        _enter_uf2(resolved)
        return {"ok": True, "requested": "uf2", "drive": _wait_for_uf2_drive(resolved)}

    return cast(dict[str, Any], await _off_loop(_enter))


@app.tool()
@meta("read", open_world=True, title="nRF52 Back Up Firmware")
async def nrf52_backup_firmware(port: str | None = None) -> dict[str, Any]:
    """Copy CURRENT.UF2 (the installed application) off a board's UF2 drive into the data dir.

    The dump restores byte-for-byte with `install`; take one before flashing. Reads
    only; the board stays in UF2 mode. Returns the backup's path.

    Args:
        port: The board's app port, ideally its `/dev/serial/by-id` path; omit when only one
            UF2 drive is mounted.
    """
    drive = await _off_loop(lambda: _uf2_drive(port))
    dest = config.mcp_data_dir() / "uf2-backups"
    result = await _off_loop(lambda: nrf52_bootloader.backup_current_uf2(drive, dest))
    if not result.get("ok"):
        raise ToolError(f"backup of {drive.get('label')} failed: {result.get('error') or result}")
    return cast(dict[str, Any], result)


def _board_serial(port: str | None) -> str | None:
    """The USB serial of `port`'s board, from its by-id name or the port listing."""
    if port is None:
        return None
    m = _BY_ID_SERIAL.search(os.path.basename(port))
    if m:
        return m.group(1)
    return nrf52_bootloader.port_usb_serial(port)


def _host_drive_error(exc: uf2_host.HostDriveError) -> ToolError:
    return ToolError(str(exc))


@app.tool()
@meta("write", open_world=True, title="nRF52 Mount UF2 Drive", port="finds_own")
async def nrf52_mount_uf2(port: str | None = None, device: str | None = None) -> dict[str, Any]:
    """Mount a board's UF2 drive on a host that does not automount it, and return its info.

    Flashing never needs it. Linux uses udisks2 (over SSH a refusal prints the one-shot polkit
    rule to allow it), macOS `diskutil`. A drive that is already mounted is returned as is.
    Returns `{mounted, drive}`.

    Args:
        port: The board's app port, ideally its `/dev/serial/by-id` path; its USB serial finds
            the drive's block device. Omit when only one UF2 drive is mounted, or pass `device`.
        device: The drive's block device (`/dev/sdb`, or `disk4` on macOS) when the port
            cannot name it.
    """

    def _mount() -> dict[str, Any]:
        try:
            return {"mounted": False, "drive": _uf2_drive(port)}
        except ToolError:
            pass
        dev = device
        if dev is None:
            serial = _board_serial(port)
            dev = uf2_host.block_device_for_serial(serial) if serial else None
        if dev is None:
            raise ToolError(
                "no UF2 block device found for that board; check it is in UF2 mode "
                "(nrf52_enter_uf2) and pass its app port, or pass device="
            )
        try:
            uf2_host.mount(dev)
        except uf2_host.HostDriveError as exc:
            raise _host_drive_error(exc) from None
        return {"mounted": True, "drive": _wait_for_uf2_drive(port) if port else _uf2_drive(None)}

    return cast(dict[str, Any], await _off_loop(_mount))


@app.tool()
@meta("destructive", open_world=True, title="nRF52 Eject UF2 Drive", port="finds_own")
async def nrf52_eject_uf2(
    ctx: Context, port: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Unmount and eject a board's UF2 drive, the way a desktop's eject does.

    For testing how the bootloader handles an eject; flashing never needs it. Linux sends udisks2
    `Drive.Eject` (over SSH a refusal prints the one-shot polkit rule to allow it), macOS
    `diskutil eject`. Requires confirm=True, or the user's answer when the client
    can ask. Returns `{ejected, label, device, steps}`.

    Args:
        port: The board's app port, ideally its `/dev/serial/by-id` path; omit when only one
            UF2 drive is mounted.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    drive = await _off_loop(lambda: _uf2_drive(port))
    pending = await confirm_mod.gate(
        ctx, f"eject the UF2 drive {drive.get('label')} at {drive.get('mount')}", confirm
    )
    if pending is not None:
        return cast(dict[str, Any], pending)

    def _eject() -> dict[str, Any]:
        try:
            steps = uf2_host.eject(drive.get("device"), drive.get("mount"))
        except uf2_host.HostDriveError as exc:
            raise _host_drive_error(exc) from None
        return {
            "ejected": True,
            "label": drive.get("label"),
            "device": drive.get("device"),
            "steps": steps,
        }

    return cast(dict[str, Any], await _off_loop(_eject))


def _check_softdevice(drive: dict[str, Any], uf2_path: str) -> dict[str, Any]:
    image = nrf52_bootloader.uf2_file_info(uf2_path)
    # A bootloader self-update image (family 0xd663823c) starts at 0 by design; the bootloader
    # moves it into place, so the application start does not apply.
    if image.get("is_bootloader_update"):
        return image
    softdevice = (drive.get("info") or {}).get("softdevice")
    start = _SD_APP_START.get(softdevice or "")
    target = image.get("target_start")
    if start is not None and target is not None and int(str(target), 16) != start:
        raise ToolError(
            f"{os.path.basename(uf2_path)} starts at {target}, but this board's {softdevice} "
            f"application starts at 0x{start:05X}; it is built for the other SoftDevice."
        )
    return image


@app.tool(**tasks_mod.task_kwargs())
@meta("destructive", open_world=True, title="nRF52 Flash UF2", port="finds_own")
async def nrf52_flash_uf2(
    ctx: Context, uf2_path: str, port: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Flash an nRF52 board by copying a .uf2 onto its bootloader UF2 drive.

    Several times faster than serial DFU, and no touch or DFU window is involved. The board
    must be in UF2 mode (`nrf52_enter_uf2`). Refuses a file that is not a UF2 and an image built
    for the other SoftDevice than the one INFO_UF2.TXT reports; a bootloader self-update image
    (`update-*.uf2`) skips that check, says `bootloader_update: true`, and asks even with
    confirm=True under `MESHTASTIC_MCP_CONFIRM=human`. Requires confirm=True, or the user's
    answer when the client can ask. Runs as a `flashes` job whose `job_id` is in the
    result and logged before the wait; follow it with `flash_poll` if the call is canceled. A
    copy the bootloader did not take is a tool error.

    Args:
        uf2_path: The .uf2 file to copy.
        port: The board's app port, ideally its `/dev/serial/by-id` path; omit when only one
            UF2 drive is mounted.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    drive = await _off_loop(lambda: _uf2_drive(port))
    image = await _off_loop(lambda: _check_softdevice(drive, uf2_path))
    bootloader = bool(image.get("is_bootloader_update"))
    what = "the bootloader update " if bootloader else ""
    pending = await confirm_mod.gate(
        ctx,
        f"flash {what}{os.path.basename(uf2_path)} onto {drive.get('label')} at "
        f"{drive.get('mount')}",
        confirm,
        irreversible=bootloader,
    )
    if pending is not None:
        return cast(dict[str, Any], pending)
    result = await _run_job(
        ctx,
        "flashes",
        str(drive.get("label")),
        lambda: _uf2_result(nrf52_bootloader.flash_uf2(drive, uf2_path, timeout_s=60)),
        # A UF2 copy finishes in seconds, and flash_poll exists only with a firmware tree.
        poll_tool=None,
    )
    return {**result, "image": image, "bootloader_update": bootloader}


def _uf2_result(res: dict[str, Any]) -> dict[str, Any]:
    # A copy the bootloader took detaches the drive; anything else did not program the board.
    ok = res.get("ok") and res.get("outcome") == "detached"
    return {**res, "exit_code": 0 if ok else 1, "stderr_tail": "" if ok else str(res)}


@app.tool()
@meta("destructive", open_world=True, title="nRF52 Factory Erase", port="finds_own")
async def nrf52_factory_erase(
    ctx: Context, port: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Wipe an nRF52 node's saved data (config, node DB, keys, BLE bonds) through its bootloader,
    leaving the firmware installed.

    Needs a bootloader whose INFO_UF2.TXT has a `Factory-Erase:` line (Meshtastic bootloader
    0.9.2-OTAFIX2.4 and later); older ones silently ignore the file, so this refuses them. The
    board must be in UF2 mode and returns to it after the erase. Requires confirm=True, or the
    user's answer when the client can ask (always, with `MESHTASTIC_MCP_CONFIRM=human`).

    Args:
        port: The board's app port, ideally its `/dev/serial/by-id` path; omit when only one
            UF2 drive is mounted.
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
    """
    drive = await _off_loop(lambda: _uf2_drive(port))
    if not ((drive.get("info") or {}).get("factory_erase") or {}).get("supported"):
        version = (drive.get("info") or {}).get("bootloader_version")
        raise ToolError(
            f"bootloader {version} on {drive.get('label')} does not accept the factory-erase "
            "file; reset or power-cycle the board back into its app and use factory_reset"
        )
    pending = await confirm_mod.gate(
        ctx,
        f"erase all saved data (config, node DB, keys) on {drive.get('label')}",
        confirm,
        irreversible=True,
    )
    if pending is not None:
        return cast(dict[str, Any], pending)

    def _erase() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            erase = Path(tmp) / "meshtastic_factory_erase.uf2"
            erase.write_bytes(nrf52_bootloader.factory_erase_uf2_bytes())
            return nrf52_bootloader.factory_erase(drive, erase)

    result = await _off_loop(_erase)
    if not result.get("ok"):
        raise ToolError(
            f"factory erase of {drive.get('label')} did not complete: "
            f"{result.get('error') or result}"
        )
    return cast(dict[str, Any], result)


# ---------- install: one tool that picks the flasher -----------------------

_INSTALL_BACK_S = 90.0  # a full ESP32 erase-and-flash can take this long to boot and answer


def _await_uf2_drive(port: str, timeout_s: float = 25.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return _uf2_drive(port)
        except ToolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)


def _await_radio(port: str) -> dict[str, Any]:
    """Wait for the flashed radio to answer, and read the firmware it now runs."""
    deadline = time.monotonic() + _INSTALL_BACK_S
    last = ""
    while time.monotonic() < deadline:
        try:
            with connection.connect(port=port, timeout_s=8.0) as iface:
                version = getattr(iface.metadata, "firmware_version", None)
            return {"back": True, "firmware_version": version}
        except connection.not_answering() as exc:
            last = str(exc)
            time.sleep(3.0)
    return {"back": False, "error": f"the radio did not answer after the install: {last}"}


def _install(
    port: str,
    source: dict[str, Any],
    erase: bool,
    userprefs: dict[str, Any] | None,
    build_flags: dict[str, Any] | None,
) -> dict[str, Any]:
    if "env" in source:
        env = str(source["env"])
        from .. import boards

        try:
            arch = str(boards.get_board(env).get("architecture") or "")
        except config.ConfigError as exc:
            raise ToolError(f"installing a build needs a firmware tree: {exc}") from exc
        except KeyError as exc:
            raise ToolError(str(exc)) from exc
        if arch.startswith("esp32"):
            method = "erase_and_flash" if erase else "update_flash"
            res = (flash.erase_and_flash if erase else flash.update_flash)(
                env, port, confirm=True, userprefs_overrides=userprefs
            )
        else:
            if erase:
                raise ToolError(
                    "erase applies to an ESP32 build; wipe an nRF52 with "
                    "admin(action='factory_reset_full') or nrf52_factory_erase"
                )
            method = "pio upload"
            res = flash.flash(
                env, port, confirm=True, userprefs_overrides=userprefs, build_flags=build_flags
            )
    else:
        path = str(source["file"])
        if not os.path.isfile(path):
            raise ToolError(f"no file at {path}")
        ext = os.path.splitext(path)[1].lower()
        if ext == ".uf2":
            method = "uf2"
            if not _uf2_drive_or_none(port):
                _enter_uf2(port)
            drive = _await_uf2_drive(port)
            _check_softdevice(drive, path)
            res = _uf2_result(nrf52_bootloader.flash_uf2(drive, path, timeout_s=60))
        elif ext == ".zip":
            method = "nrfutil dfu"
            res = hw_tools.nrfutil_dfu(port, path, confirm=True)
        elif ext == ".bin":
            # Only a factory image starts at 0x0; anything else there replaces the bootloader.
            name = os.path.basename(path)
            if not fnmatch.fnmatch(name, "firmware-*.factory.bin"):
                raise ToolError(
                    f"{name} is not a factory image, and writing it at 0x0 would replace the "
                    "bootloader: pass the release's firmware-<board>-<version>.factory.bin, "
                    "or install a build with env"
                )
            method = "esptool write_flash"
            args = ["write_flash"] + (["--erase-all"] if erase else []) + ["0x0", path]
            res = hw_tools.esptool_raw(args, port=port, confirm=True)
        else:
            raise ToolError(
                f"cannot install a {ext or 'extensionless'} file: use .uf2, .zip or .bin"
            )
    out = {**res, "method": method}
    if res.get("exit_code", 0) != 0:
        return out
    return {**out, **_await_radio(port)}


def _uf2_drive_or_none(port: str) -> dict[str, Any] | None:
    try:
        return _uf2_drive(port)
    except ToolError:
        return None


@app.tool(**tasks_mod.task_kwargs())
@meta("destructive")
async def install(
    ctx: Context,
    port: str,
    source: dict[str, Any],
    erase: bool = False,
    confirm: bool = False,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> InstallResult:
    """Install firmware on a board, picking the flasher from the board, and read back its version.

    `source` is `{"env": "<pio env>"}` (build from the firmware tree; ESP32 OTA-updates the
    app and keeps config, `erase=True` does a full erase and factory flash; nRF52 and RP2040
    upload through PlatformIO, which does the bootloader touch) or `{"file": "<path>"}`
    (`.uf2` through the nRF52 UF2 drive, entering UF2 mode first; `.zip` through nrfutil DFU;
    `.bin` written at 0x0 by esptool). After a good write it waits for the board to answer and
    returns the `firmware_version` it now reports. Requires confirm=True, or the user's answer
    when the client can ask; `erase` is irreversible. Runs as a `flashes` job: after a cancel
    or timeout follow its `job_id` with `job`, since a flash cannot be stopped mid-write.

    Returns `{job_id, method, exit_code, stdout_tail, stderr_tail, back?, firmware_version?,
    error?}`.

    Args:
        port: The board's serial port, ideally its `/dev/serial/by-id` path.
        source: `{"env": ...}` or `{"file": ...}`. A git ref or a release is not installed
            here yet: build it in the firmware tree, or download it, and pass the file.
        erase: Erase the whole flash first (ESP32 builds and `.bin` files).
        confirm: `True` once the user agreed; omitted, a client that can ask prompts the
            human.
        userprefs: `USERPREFS_*` values baked into a build.
        build_flags: `-D` macros for a build.
    """
    if len([k for k in ("env", "file") if k in source]) != 1:
        raise ToolError('pass source={"env": ...} or source={"file": ...}')
    if "ref" in source or "release" in source:
        raise ToolError(
            "installing a git ref or a release is not supported yet: the firmware tree is shared, "
            "so build that ref in your own checkout, or download the release, and pass the file"
        )
    label = source.get("env") or os.path.basename(str(source.get("file")))
    pending = await confirm_mod.gate(
        ctx,
        f"{'erase and ' if erase else ''}install {label} onto {port}",
        confirm,
        irreversible=erase,
    )
    if pending is not None:
        return cast(InstallResult, pending)
    result = await _run_job(
        ctx,
        "flashes",
        str(label),
        lambda: _install(port, source, erase, userprefs, build_flags),
        poll_tool=None,
    )
    return cast(InstallResult, result)
