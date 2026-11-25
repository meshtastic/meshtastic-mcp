# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Long-running serial monitor sessions via `pio device monitor`.

Why pio instead of raw pyserial: pio applies the board's monitor_filters —
`esp32_exception_decoder` symbolicates crash stacks, `time` adds timestamps,
etc. Raw pyserial would give us bytes; pio gives us developer-grade logs.

Each session runs `pio device monitor` in a subprocess, with a daemon reader
thread draining stdout into a bounded ring buffer. Callers pull lines via
`serial_read` using a cursor that survives across calls.

pio's monitor refuses to run unless its stdout is a tty, and Windows has no pty
to give it, so there the subprocess is a pyserial tail of the port instead: raw
lines, no monitor filters.
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import boards, config

try:  # POSIX only; pio/meshtastic-mcp targets Linux/macOS hosts.
    import pty
except ImportError:  # pragma: no cover - Windows fallback
    pty = None  # type: ignore[assignment]

_BUFFER_MAX_LINES = 10_000
_POLL_NEW_PORT_TIMEOUT_S = 3.0

# Child for hosts without pty support: argv is `port baud`, raw lines go to stdout.
_PYSERIAL_TAIL = (
    "import sys, serial\n"
    "s = serial.serial_for_url(sys.argv[1], int(sys.argv[2]))\n"
    "while True:\n"
    "    sys.stdout.buffer.write(s.readline())\n"
    "    sys.stdout.buffer.flush()\n"
)


@dataclass
class SerialSession:
    id: str
    port: str
    baud: int
    filters: list[str]
    env: str | None
    proc: subprocess.Popen
    # Master side of the pty we hand the subprocess as stdin (see open_session).
    # Kept open for the session lifetime: closing it early sends SIGHUP to the
    # child (it's holding the controlling terminal via the slave fd). None on
    # platforms without pty support (Windows) or if pty setup failed.
    _stdin_master_fd: int | None = None
    # Master side of the pty the subprocess writes stdout/stderr to. pio's
    # `is_terminal()` checks that STDOUT is a tty, so a piped stdout makes it
    # exit at once. None when pty support is missing (stdout is then a pipe).
    _stdout_master_fd: int | None = None
    _stdout_file: Any = None
    buffer: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=_BUFFER_MAX_LINES)
    )
    # Total lines seen (not bounded by buffer maxlen). `dropped = total - len(buffer)`
    # if the reader has advanced past buffer head.
    total_lines: int = 0
    started_at: float = field(default_factory=time.time)
    stopped_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None


def _drain(session: SerialSession) -> None:
    """Reader thread: line-by-line pull stdout into buffer.

    Each line is also published to the `meshtastic.serial.line` pubsub
    topic so the persistent recorder can capture it without holding its
    own port. This is the text-mode tap path: when no SerialInterface is
    open, the firmware emits full formatted lines (level + clock + uptime
    + thread + `[heap N]` prefix on DEBUG_HEAP builds + body), and we
    fan them out to whoever is listening. Pubsub is best-effort —
    publish failures must never block the reader.
    """
    # Lazy import: pubsub isn't required just to import this module
    # (e.g., during static analysis), and we want a clean test surface.
    try:
        from pubsub import pub  # type: ignore[import-untyped]
    except Exception:  # pragma: no cover - defensive
        pub = None  # type: ignore[assignment]

    source = session._stdout_file if session._stdout_file is not None else session.proc.stdout
    assert source is not None
    try:
        for line in source:
            line_stripped = line.rstrip("\r\n")
            with session.lock:
                session.buffer.append(line_stripped)
                session.total_lines += 1
            if pub is not None:
                try:
                    pub.sendMessage(
                        "meshtastic.serial.line",
                        line=line_stripped,
                        port=session.port,
                    )
                except Exception:
                    # A subscriber raising must not break the reader.
                    pass
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        session.stopped_at = time.time()


def _spawn_monitor(
    argv: list[str], cwd: str | None
) -> tuple[subprocess.Popen, int | None, int | None]:
    """Spawn the monitor child on ptys; return `(proc, stdin_master, stdout_master)`.

    `pio device monitor` needs a terminal on both ends. pyserial miniterm's
    `Console()` calls termios.tcgetattr() on stdin, and pio's `is_terminal()`
    requires STDOUT to be a tty (despite its "stdin" error message). This MCP
    server has no controlling tty (it is driven over stdio JSON-RPC), so each
    end gets a pty slave. stderr shares stdout. The child owns the slave as its
    controlling terminal (`start_new_session`), so the masters must stay open
    for the session lifetime: closing one early sends SIGHUP to the child.
    Callers close the masters (close_session does). Without pty support (Windows)
    stdin is DEVNULL and stdout a pipe, returned as `None` masters; pio refuses
    that, so open_session spawns the pyserial tail there instead.
    """
    if pty is None:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            # Boot noise is not valid UTF-8; a decode error would end the reader thread.
            errors="replace",
            bufsize=1,
        )
        return proc, None, None

    opened: list[int] = []
    try:
        stdin_master, stdin_slave = pty.openpty()
        opened += [stdin_master, stdin_slave]
        stdout_master, stdout_slave = pty.openpty()
        opened += [stdout_master, stdout_slave]
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=stdin_slave,
            stdout=stdout_slave,
            stderr=stdout_slave,
            start_new_session=True,
        )
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    # The child holds its own dup of each slave.
    os.close(stdin_slave)
    os.close(stdout_slave)
    return proc, stdin_master, stdout_master


def open_session(
    port: str,
    baud: int = 115200,
    env: str | None = None,
    filters: list[str] | None = None,
) -> SerialSession:
    """Spawn `pio device monitor` and return a SerialSession.

    If `env` is supplied, pio resolves baud and filters from platformio.ini.
    Otherwise uses the supplied `baud` and `filters` (default `['direct']`).
    Without pty support (Windows) a pyserial tail runs at the resolved baud and
    no filters apply.
    """
    # Lazy import to avoid circular: registry imports serial_session.
    from . import connection

    connection.reject_if_tcp(port, "serial_open")
    args = ["device", "monitor", "--port", port, "--no-reconnect"]
    effective_filters: list[str]
    effective_baud: int = baud
    if env is not None:
        args.extend(["-e", env])
        raw_config: dict[str, Any] = {}
        try:
            raw = boards.get_board(env).get("raw_config")
            if isinstance(raw, dict):
                raw_config = raw
        except Exception:
            raw_config = {}

        monitor_speed = raw_config.get("monitor_speed")
        has_board_speed = False
        if monitor_speed is not None:
            try:
                effective_baud = int(str(monitor_speed).strip())
                has_board_speed = True
            except (TypeError, ValueError):
                pass

        monitor_filters_raw = raw_config.get("monitor_filters")
        parsed_board_filters: list[str] = []
        if isinstance(monitor_filters_raw, str):
            for token in monitor_filters_raw.replace("\n", ",").split(","):
                item = token.strip()
                if item:
                    parsed_board_filters.append(item)
        elif isinstance(monitor_filters_raw, list):
            parsed_board_filters = [
                str(item).strip() for item in monitor_filters_raw if str(item).strip()
            ]

        has_board_filters = len(parsed_board_filters) > 0
        effective_filters = parsed_board_filters if has_board_filters else (filters or [])

        if not has_board_speed:
            args.extend(["--baud", str(effective_baud)])
        if not has_board_filters:
            for f in effective_filters:
                args.extend(["--filter", f])
    else:
        args.extend(["--baud", str(baud)])
        effective_filters = filters or ["direct"]
        for f in effective_filters:
            args.extend(["--filter", f])

    argv: list[str]
    work_dir: str | None
    if pty is None:
        # -I: a serial.py in the server's cwd must not shadow pyserial.
        argv = [sys.executable, "-I", "-c", _PYSERIAL_TAIL, port, str(effective_baud)]
        work_dir = None
        effective_filters = []
    else:
        argv = [str(config.pio_bin()), *args]
        work_dir = str(config.firmware_root())

    proc, stdin_master_fd, stdout_master_fd = _spawn_monitor(argv, work_dir)

    session = SerialSession(
        id=uuid.uuid4().hex,
        port=port,
        baud=effective_baud,
        filters=effective_filters,
        env=env,
        proc=proc,
        _stdin_master_fd=stdin_master_fd,
        _stdout_master_fd=stdout_master_fd,
    )
    if stdout_master_fd is not None:
        # closefd=False: close_session owns the fd, so the file object never double-closes.
        session._stdout_file = open(  # noqa: SIM115 - lifetime is the session's
            stdout_master_fd, buffering=1, errors="replace", closefd=False
        )
    t = threading.Thread(target=_drain, args=(session,), daemon=True)
    t.start()
    session._thread = t
    return session


def read_session(
    session: SerialSession, max_lines: int = 200, since_cursor: int | None = None
) -> dict[str, Any]:
    """Snapshot recent lines from the buffer.

    Cursor semantics: the global cursor is `total_lines` at read time. Pass
    `since_cursor` from a previous response's `new_cursor` to page forward.
    `since_cursor=0` reads everything still in the ring buffer.
    """
    with session.lock:
        total = session.total_lines
        buf_len = len(session.buffer)
        head_cursor = total - buf_len  # cursor value at buffer[0]
        current_buffer = list(session.buffer)

    if since_cursor is None:
        since_cursor = head_cursor

    # Clamp: never read what's aged out of the buffer.
    effective_start = max(since_cursor, head_cursor)
    # Number of lines skipped because they aged out between reads.
    dropped = max(0, head_cursor - since_cursor) if since_cursor < head_cursor else 0

    start_idx = effective_start - head_cursor
    end_idx = min(start_idx + max_lines, buf_len)
    lines = current_buffer[start_idx:end_idx]
    new_cursor = effective_start + len(lines)

    eof = session.proc.poll() is not None
    return {
        "lines": lines,
        "new_cursor": new_cursor,
        "eof": eof,
        "dropped": dropped,
    }


def close_session(session: SerialSession) -> bool:
    """Terminate the subprocess and join the reader thread.

    Best-effort on the reclaim path: a wedged process already got SIGKILL, so a
    post-kill `wait` timeout must not propagate and skip the thread join / state
    update (callers like `serial_close` expect a clean result, not an exception).
    """
    proc = session.proc
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass  # already SIGKILLed; reap will happen eventually
    if session._thread is not None:
        session._thread.join(timeout=3)
    session._stdout_file = None  # closefd=False; the fd is closed below
    for attr in ("_stdin_master_fd", "_stdout_master_fd"):
        fd = getattr(session, attr)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(session, attr, None)
    session.stopped_at = session.stopped_at or time.time()
    return True


def session_summary(session: SerialSession) -> dict[str, Any]:
    with session.lock:
        line_count = session.total_lines
    return {
        "session_id": session.id,
        "port": session.port,
        "baud": session.baud,
        "filters": session.filters,
        "env": session.env,
        "started_at": session.started_at,
        "stopped_at": session.stopped_at,
        "line_count": line_count,
        "eof": session.proc.poll() is not None,
    }
