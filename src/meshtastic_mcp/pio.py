# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Subprocess wrappers around the `pio` CLI.

Every PlatformIO interaction in this package funnels through `run()` so we
have a single place that owns timeouts, buffer sizes, JSON parsing, and the
"stderr on exit-0 is informational" convention.

`run()` has two execution paths:

* Fast path (default): `subprocess.run(capture_output=True)` — buffered, one
  return; fine for sub-second pio calls like `pio --version` or
  `pio project config --json-output`.
* Streaming path: when the `MESHTASTIC_MCP_FLASH_LOG` env var is set, each
  output line is tee'd to that file as it arrives via a threaded reader.
  The TUI tails the file to give live flash progress — otherwise a 3-minute
  `pio run -t upload` is completely silent to the operator.

`hw_tools.py` shares the streaming helper via `pio._run_capturing()` so
esptool/nrfutil/picotool output also streams when the env var is set.

On a thread that reports progress (a job's, or a `jobs.tracking` block's) the
output also streams, through `OutputProgress`, into that progress.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import config, jobs

# 10 MB matches the reference impl (jl-codes/platformio-mcp). Build output can
# be hundreds of KB; we'd rather keep it in memory than truncate.
_MAX_BUFFER = 10 * 1024 * 1024

# Per-operation defaults (seconds). None = no timeout.
TIMEOUT_DEFAULT = 120
TIMEOUT_PROJECT_CONFIG = 60
TIMEOUT_DEVICE_LIST = 15
TIMEOUT_BUILD = 900
TIMEOUT_UPLOAD = 600


class PioError(RuntimeError):
    """pio exited non-zero."""

    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args = tuple(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        tail = (stderr or stdout).strip().splitlines()[-20:]
        super().__init__(f"pio {' '.join(args)} failed with exit {returncode}:\n" + "\n".join(tail))


class PioTimeout(RuntimeError):
    """pio did not return within the timeout."""


@dataclass
class PioResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


_FLASH_LOG_ENV = "MESHTASTIC_MCP_FLASH_LOG"


def _flash_log_path() -> Path | None:
    """Return the path to tee subprocess output to, or None if streaming off.

    Controlled by `MESHTASTIC_MCP_FLASH_LOG`. `run-tests.sh` sets this to
    `tests/flash.log`; the TUI tails that file so `pio run -t upload` shows
    live progress in the pytest pane.
    """
    raw = os.environ.get(_FLASH_LOG_ENV)
    if not raw:
        return None
    return Path(raw)


_MESSAGE_MAX = 120
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# esptool names each image's erase range before writing it; v4 names them all up front.
_ESPTOOL_RANGE = re.compile(r"Flash will be erased from (0x[0-9a-fA-F]+) to (0x[0-9a-fA-F]+)")
# esptool v4, `Writing at 0x00010000... (45 %)`: the address of the block about to be written.
_ESPTOOL_V4 = re.compile(r"Writing at (0x[0-9a-fA-F]+)\.\.\. \((\d{1,3}) ?%\)")
# esptool v5, `Writing at 0x00012000 [===>    ]  45.2% ...`: the address written up to.
_ESPTOOL_V5 = re.compile(r"Writing at (0x[0-9a-fA-F]+) .*?(\d{1,3}(?:\.\d+)?)%")
_PICOTOOL = re.compile(
    r"((?:Loading into|Verifying) \w+|Erasing|Saving file):\s*\[[^\]]*\]\s*(\d{1,3})%"
)
# adafruit-nrfutil prints a `#` per DFU packet, 40 to a line, and no total.
_NRFUTIL_PACKETS = re.compile(r"#+")
# Lines that say nothing to a person watching: separator rules, and the JSON and dict dumps
# PlatformIO's config query and the firmware's manifest step print.
_NOISE = re.compile(r"[-=*_]{3,}|\{.*\}|\[[\[{].*")
# esptool verified the image it just wrote; the erase range rounds up to whole sectors, so the
# bytes written never reach the range's size on their own.
_ESPTOOL_VERIFIED = "Hash of data verified"


class OutputProgress:
    """Reads one run's output lines into a `jobs.Progress`.

    esptool's write counts bytes across every image it named a range for, picotool's bar counts
    percent, adafruit-nrfutil's `#` rows count packets, and any other line is just the message.
    PlatformIO's `RAM:`/`Flash:` usage bars print a percent that is not progress.
    """

    def __init__(self, progress: jobs.Progress) -> None:
        self._progress = progress
        self._lock = threading.Lock()  # the stdout and stderr readers both feed it
        self._ranges: dict[int, int] = {}
        self._reached: dict[int, float] = {}
        self._base = 0.0
        self._percent = 0.0
        self._packets = 0
        self._writing: int | None = None  # start of the esptool range being written

    def feed(self, raw: str) -> None:
        line = _ANSI.sub("", raw).strip()
        if line:
            with self._lock:
                self._read(line)

    def _read(self, line: str) -> None:
        if m := _ESPTOOL_V4.search(line):
            self._esptool(int(m[1], 16), float(m[2]), False, f"Writing at {m[1]} ({m[2]}%)")
        elif m := _ESPTOOL_V5.search(line):
            self._esptool(int(m[1], 16), float(m[2]), True, f"Writing at {m[1]} ({m[2]}%)")
        elif m := _PICOTOOL.search(line):
            self._phase(float(m[2]), f"{m[1]} ({m[2]}%)")
        elif _NRFUTIL_PACKETS.fullmatch(line):
            self._packets += len(line)
            self._progress.update(f"DFU: {self._packets} packets sent", self._packets)
        elif _ESPTOOL_VERIFIED in line and self._writing is not None:
            self._reached[self._writing] = float(self._ranges[self._writing])
            total = float(sum(self._ranges.values()))
            self._progress.update(line[:_MESSAGE_MAX], sum(self._reached.values()), total)
        elif not _NOISE.fullmatch(line):
            if m := _ESPTOOL_RANGE.search(line):
                start = int(m[1], 16)
                self._ranges[start] = int(m[2], 16) - start + 1
            self._progress.update(line[:_MESSAGE_MAX])

    def _esptool(self, address: int, percent: float, written_up_to: bool, message: str) -> None:
        for start, size in self._ranges.items():
            end = start + size
            if (start < address <= end) if written_up_to else (start <= address < end):
                done = address - start if written_up_to else size * percent / 100
                self._reached[start] = max(self._reached.get(start, 0.0), done)
                self._writing = start
                total = float(sum(self._ranges.values()))
                self._progress.update(message, sum(self._reached.values()), total)
                return
        self._phase(percent, message)

    def _phase(self, percent: float, message: str) -> None:
        """Percent of one step; a drop starts the next step, counted 100 points on."""
        if percent < self._percent:
            self._base += 100
        self._percent = percent
        self._progress.update(message, self._base + percent, self._base + 100)


def _with_progress(
    progress: jobs.Progress, line_cb: Callable[[str], None] | None
) -> Callable[[str], None]:
    reader = OutputProgress(progress)

    def cb(line: str) -> None:
        try:
            if line_cb is not None:
                line_cb(line)
        finally:
            reader.feed(line)

    return cb


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill a process started with `start_new_session=True` and every child it spawned.

    pio hands the build to scons and the compilers; killing only the leader leaves them writing
    the shared `.pio/build`.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _run_capturing(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    tee_header: str | None = None,
    extra_env: dict[str, str] | None = None,
    line_cb: Callable[[str], None] | None = None,
    tee: bool = True,
) -> tuple[int, str, str, float]:
    """Run a subprocess, capture stdout+stderr, optionally tee to the flash log.

    `tee=False` keeps this run out of the flash log.

    Returns `(returncode, stdout_str, stderr_str, duration_s)`. Raises
    `subprocess.TimeoutExpired` on timeout (callers map this to their own
    domain-specific error).

    `extra_env` merges into the subprocess environment (parent env stays
    intact). Used for `PLATFORMIO_BUILD_FLAGS=-DDEBUG_HEAP=1` and similar.

    Fast path: `subprocess.run(capture_output=True)` when no flash log is
    configured (unchanged behavior).

    Streaming path: `Popen` with line-buffered stdout+stderr pipes; two
    reader threads accumulate into result strings AND append each line to
    the flash log file. Stdout and stderr stay separate in the return value
    (so `stderr_tail` still means stderr), but are interleaved in the log
    file in the order they arrived — that's what a human wants to read.
    A thread that reports progress also takes the streaming path, so each
    line reaches its `jobs.Progress` as it arrives.
    """
    log_path = _flash_log_path() if tee else None
    t0 = time.monotonic()
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}

    interruptible = jobs.cancellable_job_running()
    progress = jobs.current_progress()
    if progress is not None:
        line_cb = _with_progress(progress, line_cb)

    if log_path is None and line_cb is None and interruptible:
        # A cancellable job gets its own process group so `jobs.cancel` stops pio's children too.
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        jobs.register_process(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            raise
        return (proc.returncode, out or "", err or "", time.monotonic() - t0)

    if log_path is None and line_cb is None:
        # Fast path — unchanged.
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return (
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            time.monotonic() - t0,
        )

    # Streaming path: line-buffered Popen, threaded readers — tee to the flash
    # log file (when `MESHTASTIC_MCP_FLASH_LOG` is set) AND/OR invoke `line_cb`
    # for live progress (FleetSuite's per-line flash/build streaming).
    log_fh: TextIO | None = None
    if log_path is not None:
        # Ensure parent directory exists so the first tee write doesn't fail.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_fh = log_path.open("a", encoding="utf-8")
        except OSError:
            pass
    # Append mode: the TUI truncates on startup, the session may produce
    # many tee'd commands (erase + flash + factory-reset response), and
    # we want all of them chronologically in one log.
    proc = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
        env=env,
        start_new_session=interruptible,
    )
    if interruptible:
        jobs.register_process(proc)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    log_lock = threading.Lock()

    def _append_log(line: str) -> None:
        # Hold the lock briefly to serialize interleaved stdout/stderr writes
        # so a half-written line from one stream doesn't get garbled by the
        # other.
        nonlocal log_fh
        with log_lock:
            if log_fh is None:
                return
            try:
                log_fh.write(line)
                log_fh.flush()
            except OSError:
                # Log file disappeared (umount, operator deleted the dir).
                # Don't let that bubble up — the subprocess output is still
                # collected in-memory for the return value.
                try:
                    log_fh.close()
                except OSError:
                    pass
                log_fh = None

    def _tee(stream, sink: list[str]) -> None:
        try:
            for line in stream:
                sink.append(line)
                _append_log(line)
                if line_cb is not None:
                    try:
                        line_cb(line.rstrip("\n"))
                    except Exception:
                        pass
        except Exception:
            pass

    # Header line so the operator can tell commands apart in the log.
    if tee_header:
        _append_log(f"\n--- {tee_header} (start)\n")

    assert proc.stdout is not None and proc.stderr is not None
    t_out = threading.Thread(target=_tee, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_tee, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    # `Popen.wait` with a timeout is the cleanest way to get TimeoutExpired.
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if interruptible:
            _kill_group(proc)
        else:
            proc.kill()
        proc.wait()
        # Drain readers before re-raising so we don't leave threads behind.
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        raise

    if timeout is not None:
        # A child that outlives the tool can hold the pipes open; the timeout covers that too.
        for reader in (t_out, t_err):
            reader.join(max(0.0, t0 + timeout - time.monotonic()))
        if t_out.is_alive() or t_err.is_alive():
            raise subprocess.TimeoutExpired(list(argv), timeout)
    t_out.join()
    t_err.join()
    duration = time.monotonic() - t0

    if tee_header:
        _append_log(f"--- {tee_header} (exit {proc.returncode} in {duration:.1f}s)\n")

    try:
        return (
            proc.returncode,
            "".join(stdout_chunks),
            "".join(stderr_chunks),
            duration,
        )
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = TIMEOUT_DEFAULT,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    line_cb: Callable[[str], None] | None = None,
) -> PioResult:
    """Invoke `pio <args>` and return captured output.

    `cwd` defaults to the firmware root. `check=True` raises `PioError` on
    non-zero exit; set `check=False` to inspect `returncode` manually.

    `extra_env` merges into the subprocess environment — used for
    `PLATFORMIO_BUILD_FLAGS=-DDEBUG_HEAP=1` and similar build-time
    toggles that can't be expressed as command-line args.

    If `MESHTASTIC_MCP_FLASH_LOG` is set, output is also tee'd to that file
    line-by-line as it arrives (for live flash progress in the TUI).
    """
    binary = str(config.pio_bin())
    work_dir = cwd or config.firmware_root()
    full = [binary, *args]
    try:
        rc, stdout, stderr, duration = _run_capturing(
            full,
            cwd=work_dir,
            timeout=timeout,
            tee_header=f"pio {' '.join(args)}",
            extra_env=extra_env,
            line_cb=line_cb,
        )
    except subprocess.TimeoutExpired as exc:
        raise PioTimeout(f"pio {' '.join(args)} timed out after {timeout}s") from exc

    result = PioResult(
        args=list(args),
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        duration_s=duration,
    )
    if check and result.returncode != 0:
        raise PioError(args, result.returncode, result.stdout, result.stderr)
    return result


def run_json(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = TIMEOUT_DEFAULT,
):
    """Run pio with `--json-output` appended and parse the result."""
    full = list(args)
    if "--json-output" not in full:
        full.append("--json-output")
    res = run(full, cwd=cwd, timeout=timeout, check=True)
    if not res.stdout.strip():
        raise PioError(args, 0, res.stdout, res.stderr or "pio returned empty output")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise PioError(args, 0, res.stdout[:2000], f"invalid JSON from pio: {exc}") from exc


def tail_lines(text: str, n: int = 200) -> str:
    """Last `n` lines of `text`, joined with newlines. Empty string stays empty."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])
