# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Long-running serial monitor sessions via `pio device monitor`.

Why pio instead of raw pyserial: pio applies the board's monitor_filters —
`esp32_exception_decoder` symbolicates crash stacks, `time` adds timestamps,
etc. Raw pyserial would give us bytes; pio gives us developer-grade logs.

Each session runs `pio device monitor` in a subprocess, with a daemon reader
thread draining stdout into a bounded ring buffer. Callers pull lines via
`serial_read` using a cursor that survives across calls.
"""

from __future__ import annotations

import collections
import os
import subprocess
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

    assert session.proc.stdout is not None
    try:
        for line in session.proc.stdout:
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


def open_session(
    port: str,
    baud: int = 115200,
    env: str | None = None,
    filters: list[str] | None = None,
) -> SerialSession:
    """Spawn `pio device monitor` and return a SerialSession.

    If `env` is supplied, pio resolves baud and filters from platformio.ini.
    Otherwise uses the supplied `baud` and `filters` (default `['direct']`).
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

    binary = str(config.pio_bin())
    work_dir = str(config.firmware_root())

    # `pio device monitor` -> pyserial miniterm builds a `Console()` that calls
    # termios.tcgetattr() on its stdin unconditionally (even though we never
    # send it keystrokes). When this MCP server itself has no controlling tty
    # (the normal case — it's driven over stdio JSON-RPC, not a terminal), the
    # inherited stdin is a pipe/socket and tcgetattr raises
    # `termios.error: (25, 'Inappropriate ioctl for device')`, crashing pio
    # before it ever opens the serial port. Fix: hand the child a pty slave as
    # stdin instead of inheriting ours — it satisfies tcgetattr without
    # requiring a real interactive terminal. The master fd is kept open for
    # the session's lifetime (closing it early delivers SIGHUP to the child,
    # which owns the slave as its controlling terminal via start_new_session).
    stdin_master_fd: int | None = None
    popen_kwargs: dict[str, Any] = {}
    if pty is not None:
        try:
            stdin_master_fd, slave_fd = pty.openpty()
        except OSError:
            stdin_master_fd = None
    if stdin_master_fd is not None:
        popen_kwargs["stdin"] = slave_fd
        popen_kwargs["start_new_session"] = True  # slave becomes the child's controlling tty
    else:
        popen_kwargs["stdin"] = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            [binary, *args],
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
            **popen_kwargs,
        )
    finally:
        if stdin_master_fd is not None:
            # Parent doesn't need the slave once the child has dup'd it onto fd 0.
            os.close(slave_fd)

    session = SerialSession(
        id=uuid.uuid4().hex,
        port=port,
        baud=effective_baud,
        filters=effective_filters,
        env=env,
        proc=proc,
        _stdin_master_fd=stdin_master_fd,
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
    if session._stdin_master_fd is not None:
        try:
            os.close(session._stdin_master_fd)
        except OSError:
            pass
        session._stdin_master_fd = None
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
