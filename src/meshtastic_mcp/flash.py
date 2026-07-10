# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Build, clean, flash, and bootloader-entry operations.

Design: pio is the preferred path for every architecture via `flash()`. For
ESP32 factory flashes we shell out to `bin/device-install.sh` (which knows
about partition offsets and the OTA/littlefs partitions); for ESP32 OTA
updates we use `bin/device-update.sh`. Both scripts require the build
artifacts to exist, so these tools build first if needed.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import serial

from . import boards, config, connection, devices, pio, port_recovery, userprefs

# Meshtastic variants use both `esp32s3` and `esp32-s3` style names across
# variants/*/platformio.ini (no consistency enforced). Accept both spellings.
ESP32_ARCHES = {
    "esp32",
    "esp32s2",
    "esp32-s2",
    "esp32s3",
    "esp32-s3",
    "esp32c3",
    "esp32-c3",
    "esp32c6",
    "esp32-c6",
}


class FlashError(RuntimeError):
    pass


def _require_confirm(confirm: bool, operation: str) -> None:
    if not confirm:
        raise FlashError(
            f"{operation} is destructive and requires confirm=True. "
            "This will overwrite firmware on the device."
        )


def _reject_native_env(env: str, operation: str) -> None:
    """`native*` envs build a host executable, not firmware — there's no
    upload step. The user wants `build` (or just runs the binary directly).
    """
    if env.startswith("native"):
        raise FlashError(
            f"{operation} is not applicable for env {env!r}: native envs "
            "produce a host executable, not flashable firmware. Use `build` "
            "instead, then run the resulting binary directly."
        )


def _artifacts_for(env: str) -> list[Path]:
    build_dir = config.firmware_root() / ".pio" / "build" / env
    if not build_dir.is_dir():
        return []
    patterns = (
        "firmware*.bin",
        "firmware*.uf2",
        "firmware*.hex",
        "firmware*.zip",
        "firmware*.elf",
        "*.mt.json",
        "littlefs-*.bin",
    )
    out: list[Path] = []
    for pat in patterns:
        out.extend(sorted(build_dir.glob(pat)))
    return out


def _factory_bin_for(env: str) -> Path | None:
    build_dir = config.firmware_root() / ".pio" / "build" / env
    if not build_dir.is_dir():
        return None
    matches = sorted(build_dir.glob("firmware-*.factory.bin"))
    return matches[0] if matches else None


def _firmware_bin_for(env: str) -> Path | None:
    """Return the OTA-update firmware binary (app partition only)."""
    build_dir = config.firmware_root() / ".pio" / "build" / env
    if not build_dir.is_dir():
        return None
    # device-update.sh expects firmware-<env>-<version>.bin (not .factory.bin)
    matches = sorted(
        p for p in build_dir.glob("firmware-*.bin") if not p.name.endswith(".factory.bin")
    )
    return matches[0] if matches else None


def _userprefs_summary(active: dict[str, str]) -> dict[str, Any]:
    """Compact summary of which USERPREFS_* are baked into the build."""
    return {"count": len(active), "keys": sorted(active.keys())}


def build(
    env: str,
    with_manifest: bool = True,
    userprefs_overrides: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `pio run -e <env>` and return artifact paths.

    `userprefs_overrides` (optional): dict of `USERPREFS_<KEY>: value` to inject
    into userPrefs.jsonc for this build only. File is restored byte-for-byte
    on exit. Use `userprefs_set()` for persistent changes.

    `build_flags` (optional): dict of `-D<NAME>=<VALUE>` macros to set for
    this build only via `PLATFORMIO_BUILD_FLAGS`. Common useful flag:
    `{"DEBUG_HEAP": 1}` enables per-thread leak detection + `[heap N]`
    prefix on every log line. Combines with the recorder so heap shows
    up at log cadence (much higher resolution than the ~60 s LocalStats
    packet) — see `recorder/parsers.py:_HEAP_PREFIX_RE`. Bool values
    expand to bare `-D<NAME>` (presence-only flags).
    """
    args = ["run", "-e", env]
    if with_manifest:
        args.extend(["-t", "mtjson"])
    extra_env = _build_flags_env(build_flags) if build_flags else None
    with userprefs.temporary_overrides(userprefs_overrides) as effective:
        result = pio.run(
            args,
            timeout=pio.TIMEOUT_BUILD,
            check=False,
            extra_env=extra_env,
        )
    return {
        "exit_code": result.returncode,
        "artifacts": [str(p) for p in _artifacts_for(env)],
        "stdout_tail": pio.tail_lines(result.stdout, 200),
        "stderr_tail": pio.tail_lines(result.stderr, 200),
        "duration_s": round(result.duration_s, 2),
        "userprefs": _userprefs_summary(effective),
        "build_flags": dict(build_flags) if build_flags else None,
    }


def _build_flags_env(build_flags: dict[str, Any]) -> dict[str, str]:
    """Translate `{"DEBUG_HEAP": 1, "FOO": "bar"}` → `{"PLATFORMIO_BUILD_FLAGS":
    "-DDEBUG_HEAP=1 -DFOO=bar"}`. Bool True → bare `-D<NAME>`; False/None drop
    the flag entirely. Other types stringify."""
    parts: list[str] = []
    for key, value in build_flags.items():
        if value is False or value is None:
            continue
        if value is True:
            parts.append(f"-D{key}")
        else:
            parts.append(f"-D{key}={value}")
    if not parts:
        return {}
    return {"PLATFORMIO_BUILD_FLAGS": " ".join(parts)}


def clean(env: str) -> dict[str, Any]:
    """Run `pio run -e <env> -t clean`."""
    result = pio.run(["run", "-e", env, "-t", "clean"], timeout=120, check=False)
    return {
        "exit_code": result.returncode,
        "stdout_tail": pio.tail_lines(result.stdout, 200),
        "stderr_tail": pio.tail_lines(result.stderr, 200),
        "duration_s": round(result.duration_s, 2),
    }


# adafruit-nrfutil prints one of these when a serial-DFU upload fails to program
# the device — but `pio run -t upload` STILL exits 0, so a flash that uploaded
# NOTHING looks like a success. Every caller that trusts the exit code (the
# bake's `assert exit_code == 0`, FleetSuite's /flash + /recover reflash) then
# records a phantom flash and marks an unprovisioned board as baked. Detect the
# signature and fail loudly instead. (The strings are stable nrfutil output and
# don't appear on a real upload, so the match is low-risk.)
_UPLOAD_FAILURE_MARKERS = (
    "Failed to upgrade target",
    "Target is not in DFU mode",
    "No data received on serial port",
)


def _detect_upload_failure(stdout: str | None, stderr: str | None) -> str | None:
    """Return the upload-failure marker present in the pio output, or None.

    Catches the case where the nRF52 DFU upload silently failed but pio reported
    success — so callers can treat it as the failure it actually was."""
    blob = f"{stdout or ''}\n{stderr or ''}"
    for marker in _UPLOAD_FAILURE_MARKERS:
        if marker in blob:
            return marker
    return None


def flash(
    env: str,
    port: str,
    confirm: bool = False,
    userprefs_overrides: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """`pio run -e <env> -t upload --upload-port <port>`. All architectures.

    `userprefs_overrides` (optional): see `build()` — the rebuild-before-upload
    that pio performs will pick up the injected values.

    `build_flags` (optional): same shape as `build()` — `PLATFORMIO_BUILD_FLAGS`
    is exported for the rebuild-before-upload, so the uploaded firmware
    actually carries the flags. Without this propagation, `pio run -t upload`
    would relink without the env var and silently drop them. Common use:
    `build_flags={"DEBUG_HEAP": 1}` for the leak-hunt path.

    `progress_cb` (optional): invoked with each pio output line as it arrives
    (forwarded to `pio.run`'s `line_cb`), so a caller can stream live
    compile/upload progress without waiting for the multi-minute run to finish.

    Pre-flight: the port is run through `port_recovery.ensure_port_free` first so
    a held or wedged device auto-recovers before the upload (see body). A
    `PortRecoveryError` there is surfaced as a `FlashError`.
    """
    _require_confirm(confirm, "flash")
    _reject_native_env(env, "flash")
    connection.reject_if_tcp(port, "flash")
    # Pre-flight: a held or wedged port can't be flashed. ensure_port_free waits
    # out a transient holder and, if the device is genuinely wedged (hung
    # firmware / stale CDC node), power-cycles its own hub slot to re-enumerate
    # it. Re-enumeration can bring the device back on a DIFFERENT /dev path, so
    # flash whatever path it returns rather than the one we were handed.
    try:
        port = port_recovery.ensure_port_free(port, allow_power_cycle=True)
    except port_recovery.PortRecoveryError as exc:
        raise FlashError(
            f"cannot flash {env!r}: serial port {port} could not be made usable ({exc})"
        ) from exc
    extra_env = _build_flags_env(build_flags) if build_flags else None
    with userprefs.temporary_overrides(userprefs_overrides) as effective:
        result = pio.run(
            ["run", "-e", env, "-t", "upload", "--upload-port", port],
            timeout=pio.TIMEOUT_UPLOAD,
            check=False,
            extra_env=extra_env,
            line_cb=progress_cb,
        )
    upload_error = _detect_upload_failure(result.stdout, result.stderr)
    exit_code = result.returncode
    if upload_error and exit_code == 0:
        # pio masked a silent DFU failure as success — surface it as non-zero so
        # the bake/flash/recover paths don't record a flash that never landed.
        exit_code = 1
    out = {
        "exit_code": exit_code,
        "stdout_tail": pio.tail_lines(result.stdout, 200),
        "stderr_tail": pio.tail_lines(result.stderr, 200),
        "duration_s": round(result.duration_s, 2),
        "userprefs": _userprefs_summary(effective),
        "build_flags": dict(build_flags) if build_flags else None,
    }
    if upload_error:
        out["upload_error"] = upload_error
    return out


def _check_esp32_env(env: str) -> str:
    rec = boards.get_board(env)
    arch = rec.get("architecture")
    if arch not in ESP32_ARCHES:
        raise FlashError(
            f"Env {env!r} has architecture {arch!r}, not ESP32. Use `flash` for non-ESP32 boards."
        )
    return arch


def _run_install_script(script: Path, port: str, binary: Path) -> dict[str, Any]:
    """Invoke bin/device-install.sh or bin/device-update.sh."""
    # The scripts invoke `esptool` from PATH, which may not carry the venv /
    # PlatformIO location our resolver finds — prepend it so they agree.
    esptool_dir = str(config.esptool_bin().parent)
    env = os.environ.copy()
    existing_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(part for part in (esptool_dir, existing_path) if part)

    t0 = time.monotonic()
    proc = subprocess.run(
        [str(script), "-p", port, "-f", str(binary)],
        cwd=str(config.firmware_root()),
        capture_output=True,
        text=True,
        timeout=pio.TIMEOUT_UPLOAD,
        env=env,
    )
    duration = time.monotonic() - t0
    return {
        "exit_code": proc.returncode,
        "stdout_tail": pio.tail_lines(proc.stdout, 200),
        "stderr_tail": pio.tail_lines(proc.stderr, 200),
        "duration_s": round(duration, 2),
    }


def erase_and_flash(
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32-only: full erase + factory flash via bin/device-install.sh.

    `userprefs_overrides`: baked into the factory.bin via a fresh build. If
    overrides are provided we always force a rebuild (skip_build=True errors
    in that case) since a cached factory.bin would not reflect the new prefs.
    """
    _require_confirm(confirm, "erase_and_flash")
    connection.reject_if_tcp(port, "erase_and_flash")
    _check_esp32_env(env)

    if userprefs_overrides and skip_build:
        raise FlashError(
            "userprefs_overrides forces a rebuild so the factory.bin reflects "
            "the new values; skip_build=True is incompatible."
        )

    with userprefs.temporary_overrides(userprefs_overrides) as effective:
        # If overrides were provided, always build; otherwise only build if
        # no factory.bin is present.
        factory = _factory_bin_for(env)
        if factory is None or userprefs_overrides:
            if skip_build:
                raise FlashError(
                    f"No factory.bin found for env {env!r} and skip_build=True. "
                    "Run `build` first or set skip_build=False."
                )
            build_args = ["run", "-e", env, "-t", "mtjson"]
            build_result = pio.run(build_args, timeout=pio.TIMEOUT_BUILD, check=False)
            if build_result.returncode != 0:
                return {
                    "exit_code": build_result.returncode,
                    "stdout_tail": pio.tail_lines(build_result.stdout, 200),
                    "stderr_tail": pio.tail_lines(build_result.stderr, 200),
                    "duration_s": round(build_result.duration_s, 2),
                    "error": "build failed before erase_and_flash could run",
                    "userprefs": _userprefs_summary(effective),
                }
            factory = _factory_bin_for(env)
            if factory is None:
                raise FlashError(
                    f"Build succeeded but no factory.bin appeared in .pio/build/{env}/"
                )

        script = config.firmware_root() / "bin" / "device-install.sh"
        if not script.is_file():
            raise FlashError(f"device-install.sh not found at {script}")
        result = _run_install_script(script, port, factory)

    result["userprefs"] = _userprefs_summary(effective)
    return result


def update_flash(
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32-only: OTA app-partition update via bin/device-update.sh.

    `userprefs_overrides`: baked into the firmware.bin via a fresh build. If
    overrides are provided we always force a rebuild.
    """
    _require_confirm(confirm, "update_flash")
    connection.reject_if_tcp(port, "update_flash")
    _check_esp32_env(env)

    if userprefs_overrides and skip_build:
        raise FlashError(
            "userprefs_overrides forces a rebuild so the firmware.bin reflects "
            "the new values; skip_build=True is incompatible."
        )

    with userprefs.temporary_overrides(userprefs_overrides) as effective:
        firmware = _firmware_bin_for(env)
        if firmware is None or userprefs_overrides:
            if skip_build:
                raise FlashError(
                    f"No firmware.bin found for env {env!r} and skip_build=True. "
                    "Run `build` first or set skip_build=False."
                )
            build_args = ["run", "-e", env, "-t", "mtjson"]
            build_result = pio.run(build_args, timeout=pio.TIMEOUT_BUILD, check=False)
            if build_result.returncode != 0:
                return {
                    "exit_code": build_result.returncode,
                    "stdout_tail": pio.tail_lines(build_result.stdout, 200),
                    "stderr_tail": pio.tail_lines(build_result.stderr, 200),
                    "duration_s": round(build_result.duration_s, 2),
                    "error": "build failed before update_flash could run",
                    "userprefs": _userprefs_summary(effective),
                }
            firmware = _firmware_bin_for(env)
            if firmware is None:
                raise FlashError(
                    f"Build succeeded but no firmware.bin appeared in .pio/build/{env}/"
                )

        script = config.firmware_root() / "bin" / "device-update.sh"
        if not script.is_file():
            raise FlashError(f"device-update.sh not found at {script}")
        result = _run_install_script(script, port, firmware)

    result["userprefs"] = _userprefs_summary(effective)
    return result


def _do_1200bps_touch(port: str, settle_ms: int, touch_timeout_s: float = 3.0) -> None:
    """Open port at 1200 baud and close, bounded by a worker thread.

    Both the open and the close can block on a busy CDC device — we wrap the
    whole thing in a worker so the caller returns in at most `touch_timeout_s`
    regardless. The touch is signal-only: the USB configuration change to
    1200 baud alone is enough to trip the Adafruit bootloader's reset, so a
    worker that's still blocked in the background after timeout has already
    delivered the signal.
    """
    errors: list[BaseException] = []

    def _inner() -> None:
        try:
            s = serial.Serial(port, 1200)
        except serial.SerialException as exc:
            if "No such file" in str(exc) or "could not open" in str(exc).lower():
                raise
            return  # other serial errors mid-open are expected during DFU entry
        try:
            time.sleep(settle_ms / 1000.0)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _runner() -> None:
        try:
            _inner()
        except BaseException as exc:  # re-raised on caller thread after join
            errors.append(exc)

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout=touch_timeout_s)
    if worker.is_alive():
        return  # signal already delivered; allow daemon worker to finish/exit
    if errors:
        raise errors[0]


# Adafruit nRF52 bootloader VID/PID (BOTH RAK4631 and most Feather nRF52 boards).
# See https://github.com/adafruit/Adafruit_nRF52_Bootloader
_NRF52_BOOTLOADER_VID = 0x239A
_NRF52_BOOTLOADER_PIDS = {
    0x0029,  # Adafruit nRF52 bootloader (generic, used by RAK4631)
    0x002A,  # Adafruit Feather Express bootloader variant
    0x4029,  # alt seen on some boards
}


def _parse_hex(value: Any) -> int | None:
    """Parse a ``"0x239a"``-style hex string (or raw int) to an int, or None."""
    if value is None:
        return None
    try:
        return int(value, 16) if isinstance(value, str) else int(value)
    except (ValueError, TypeError):
        return None


def _find_nrf52_bootloader_port(
    before_pids: dict[str, int | None] | None = None,
) -> dict[str, Any] | None:
    """Return a port genuinely in Adafruit nRF52 *serial-DFU* mode, or None.

    A candidate must present the bootloader VID/PID (0x239A / one of
    ``_NRF52_BOOTLOADER_PIDS``). When ``before_pids`` (a pre-touch
    ``{port: pid_int}`` map captured before the 1200bps touch) is supplied, the
    candidate must ALSO represent a real re-enumeration into DFU: either the
    port did not exist before the touch, or its PID changed.

    Without that before-state check a wedged app-mode board can be misreported
    as flashable: a LilyGO T-Echo (0x239A/0x002A) sits on the USB bus with a
    VID/PID that collides with a bootloader PID, yet it never entered DFU — so
    nrfutil would fail with "Target is not in DFU mode". Requiring a new port or
    a PID change rejects that false positive.
    """
    for d in devices.list_devices(include_unknown=True):
        vid = _parse_hex(d.get("vid"))
        pid = _parse_hex(d.get("pid"))
        if vid is None or pid is None:
            continue
        if vid != _NRF52_BOOTLOADER_VID or pid not in _NRF52_BOOTLOADER_PIDS:
            continue
        # If the port already existed before the touch, only a PID change is a
        # genuine app→DFU re-enumeration. An unchanged PID means we're still
        # looking at the same app-mode (or wedged) port, not a bootloader.
        if before_pids is not None and d["port"] in before_pids and before_pids[d["port"]] == pid:
            continue
        return d
    return None


def touch_1200bps(
    port: str,
    settle_ms: int = 250,
    poll_timeout_s: float = 8.0,
    retries: int = 2,
) -> dict[str, Any]:
    """Open port at 1200 baud, close immediately — triggers USB CDC bootloader.

    Works for: nRF52840 (Adafruit bootloader), ESP32-S3 (native USB download
    mode), RP2040 (when built with 1200bps-reset stdio), Arduino Leonardo/Micro.

    For nRF52 specifically: after the touch, polls for the Adafruit bootloader
    VID/PID (0x239A / 0x0029) for up to `poll_timeout_s` seconds. Adafruit's
    bootloader docs note a touch sometimes needs to be repeated, so this
    retries up to `retries` times. The returned `new_port` is the bootloader
    port (distinct from the app port) — exactly what's needed for `pio run
    -t upload` to drive nrfutil.

    For non-nRF52 devices (ESP32-S3, RP2040, Arduino), falls back to
    "any-new-port appeared" detection.

    Returns `{ok, former_port, new_port, new_port_vid_pid, attempts}`.
    """
    connection.reject_if_tcp(port, "touch_1200bps")
    before_list = devices.list_devices(include_unknown=True)
    before_ports = {d["port"] for d in before_list}
    # Pre-touch VID/PID per port, so we can distinguish a genuine DFU
    # re-enumeration (PID changed at a known port, or a brand-new port) from an
    # app-mode/wedged port that merely carries a VID/PID colliding with a
    # bootloader PID (e.g. a wedged T-Echo at 0x239A/0x002A).
    before_pids = {d["port"]: _parse_hex(d.get("pid")) for d in before_list}

    attempts = 0
    new_port_info: dict[str, Any] | None = None

    for attempt in range(1, retries + 1):
        attempts = attempt
        _do_1200bps_touch(port, settle_ms=settle_ms, touch_timeout_s=3.0)

        # Poll for either (a) the nRF52 bootloader VID/PID appearing as a real
        # DFU re-enumeration (new port, or changed PID), or (b) a brand-new port
        # appearing that wasn't there before.
        deadline = time.monotonic() + poll_timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.2)

            bootloader = _find_nrf52_bootloader_port(before_pids=before_pids)
            if bootloader is not None:
                new_port_info = bootloader
                break

            current = devices.list_devices(include_unknown=True)
            current_paths = {d["port"] for d in current}
            added = current_paths - before_ports
            if added:
                added_record = next((d for d in current if d["port"] in added), None)
                if added_record:
                    new_port_info = added_record
                    break

        if new_port_info is not None:
            break
        # No bootloader appeared; try touching again (Adafruit recommends
        # sometimes requiring two touches for reliability).

    if new_port_info is not None:
        return {
            "ok": True,
            "former_port": port,
            "new_port": new_port_info["port"],
            "new_port_vid_pid": (
                new_port_info.get("vid"),
                new_port_info.get("pid"),
            ),
            "attempts": attempts,
        }

    return {
        "ok": False,
        "former_port": port,
        "new_port": None,
        "new_port_vid_pid": (None, None),
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Async (non-blocking) jobs — for MCP clients with short per-call timeouts.
# Both build and flash exceed the typical 60 s MCP request timeout; run them
# in a daemon thread, return a job_id immediately, poll for completion.
# ---------------------------------------------------------------------------

_active_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job_data_dir(kind: str) -> Path:
    import os

    from platformdirs import user_data_dir

    root = Path(os.environ.get("MESHTASTIC_MCP_DATA_DIR") or user_data_dir("meshtastic-mcp"))
    d = root / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _start_job(kind: str, env: str, worker_body) -> dict[str, Any]:
    """Launch `worker_body(state, log_path)` in a daemon thread, tracked by job_id.

    `kind` is "builds" or "flashes" (used for the log subdir and id prefix).
    `worker_body` receives the mutable `state` dict + the log Path; it runs the
    actual pio invocation and updates state under `_jobs_lock`.
    """
    import uuid

    job_id = uuid.uuid4().hex[:12]
    log_path = _job_data_dir(kind) / f"{job_id}.log"

    state: dict[str, Any] = {
        "job_id": job_id,
        "kind": kind,
        "env": env,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "artifacts": [],
        "log_path": str(log_path),
    }
    with _jobs_lock:
        _active_jobs[job_id] = state

    def _run() -> None:
        try:
            worker_body(state, log_path)
        except Exception as exc:
            log_path.write_text(f"{kind} worker error: {exc}\n", encoding="utf-8")
            with _jobs_lock:
                state["status"] = "failed"
                state["finished_at"] = time.time()
                state["error"] = str(exc)

    threading.Thread(target=_run, daemon=True, name=f"{kind}-{job_id}").start()
    return {"job_id": job_id, "status": "running", "log_path": str(log_path)}


def _poll_job(job_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Shared poll for build/flash jobs started via `_start_job`."""
    with _jobs_lock:
        state = _active_jobs.get(job_id)
    if state is None:
        return {"error": f"Unknown job_id {job_id!r} (only this session's jobs are tracked)."}

    log_path = Path(state["log_path"])
    log_tail: list[str] = []
    if log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]

    elapsed = round((state["finished_at"] or time.time()) - state["started_at"], 1)
    return {
        "job_id": job_id,
        "kind": state["kind"],
        "env": state["env"],
        "status": state["status"],
        "elapsed_s": elapsed,
        "exit_code": state.get("exit_code"),
        "duration_s": state.get("duration_s"),
        "artifacts": state.get("artifacts", []),
        "log_tail": log_tail,
        "log_path": state["log_path"],
    }


def build_start(
    env: str,
    with_manifest: bool = True,
    userprefs_overrides: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch a firmware build in the background and return a job_id immediately.

    The build runs in a daemon thread so this call returns in under a second.
    Poll with `build_poll(job_id)` to check status and retrieve output.
    """

    def _body(state: dict[str, Any], log_path: Path) -> None:
        args = ["run", "-e", env]
        if with_manifest:
            args.extend(["-t", "mtjson"])
        extra_env = _build_flags_env(build_flags) if build_flags else None
        with userprefs.temporary_overrides(userprefs_overrides):
            result = pio.run(args, timeout=pio.TIMEOUT_BUILD, check=False, extra_env=extra_env)
        stderr_section = "\n--- stderr ---\n" + result.stderr if result.stderr.strip() else ""
        log_path.write_text(result.stdout + stderr_section, encoding="utf-8")
        with _jobs_lock:
            state["status"] = "done" if result.returncode == 0 else "failed"
            state["exit_code"] = result.returncode
            state["finished_at"] = time.time()
            state["duration_s"] = round(result.duration_s, 2)
            state["artifacts"] = [str(p) for p in _artifacts_for(env)]

    out = _start_job("builds", env, _body)
    # Back-compat alias: callers/tests historically read build_id.
    out["build_id"] = out["job_id"]
    return out


def build_poll(build_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Check status of a background build started with `build_start`.

    Returns status (running/done/failed), elapsed time, artifacts, and the
    last `tail_lines` lines of build output.
    """
    out = _poll_job(build_id, tail_lines=tail_lines)
    if "job_id" in out:
        out["build_id"] = out["job_id"]
    return out


def flash_start(
    env: str,
    port: str,
    confirm: bool = False,
    userprefs_overrides: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch a firmware flash in the background and return a job_id immediately.

    Mirrors `build_start` for the upload step, which also exceeds the typical
    60 s MCP request timeout. Requires confirm=True. Poll with `flash_poll`.
    """
    _require_confirm(confirm, "flash_start")
    _reject_native_env(env, "flash_start")
    connection.reject_if_tcp(port, "flash_start")

    def _body(state: dict[str, Any], log_path: Path) -> None:
        extra_env = _build_flags_env(build_flags) if build_flags else None
        with userprefs.temporary_overrides(userprefs_overrides):
            result = pio.run(
                ["run", "-e", env, "-t", "upload", "--upload-port", port],
                timeout=pio.TIMEOUT_UPLOAD,
                check=False,
                extra_env=extra_env,
            )
        stderr_section = "\n--- stderr ---\n" + result.stderr if result.stderr.strip() else ""
        log_path.write_text(result.stdout + stderr_section, encoding="utf-8")
        with _jobs_lock:
            state["status"] = "done" if result.returncode == 0 else "failed"
            state["exit_code"] = result.returncode
            state["finished_at"] = time.time()
            state["duration_s"] = round(result.duration_s, 2)
            state["port"] = port

    return _start_job("flashes", env, _body)


def flash_poll(job_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Check status of a background flash started with `flash_start`."""
    return _poll_job(job_id, tail_lines=tail_lines)
