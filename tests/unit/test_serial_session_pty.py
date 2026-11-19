# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The monitor child must see a tty on stdin and stdout, and its output must be drained."""

from __future__ import annotations

import os
import sys
import time

import pytest

from meshtastic_mcp import serial_session

pytestmark = pytest.mark.skipif(serial_session.pty is None, reason="needs pty support")

_CHILD = (
    "import os, sys; "
    "print('stdin_tty=%s stdout_tty=%s stderr_tty=%s' % "
    "(os.isatty(0), os.isatty(1), os.isatty(2)), flush=True); "
    "import time; time.sleep(30)"
)


def _wait(pred, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_spawn_gives_child_a_tty_on_stdin_and_stdout(tmp_path) -> None:
    proc, stdin_master, stdout_master = serial_session._spawn_monitor(
        [sys.executable, "-c", _CHILD], str(tmp_path)
    )
    try:
        assert stdin_master is not None and stdout_master is not None
        out = os.read(stdout_master, 4096).decode()  # blocks until the child prints
        assert "stdin_tty=True stdout_tty=True stderr_tty=True" in out
    finally:
        proc.kill()
        proc.wait(timeout=10)
        for fd in (stdin_master, stdout_master):
            if fd is not None:
                os.close(fd)


def test_session_drains_pty_output_and_closes_fds(tmp_path, monkeypatch) -> None:
    from meshtastic_mcp import config, connection

    monkeypatch.setattr(connection, "reject_if_tcp", lambda *a, **k: None)
    monkeypatch.setattr(config, "firmware_root", lambda: tmp_path)
    # The "pio" binary is a launcher that runs the tty probe, ignoring pio's args.
    launcher = tmp_path / "fakepio"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} -c "{_CHILD}"\n')
    launcher.chmod(0o755)
    monkeypatch.setattr(config, "pio_bin", lambda: launcher)

    session = serial_session.open_session("/dev/fake", baud=115200)
    stdin_fd, stdout_fd = session._stdin_master_fd, session._stdout_master_fd
    assert stdin_fd is not None and stdout_fd is not None
    try:
        assert _wait(lambda: session.total_lines >= 1)
        res = serial_session.read_session(session, since_cursor=0)
        assert res["lines"][0] == "stdin_tty=True stdout_tty=True stderr_tty=True"
    finally:
        serial_session.close_session(session)

    assert session._stdin_master_fd is None and session._stdout_master_fd is None
    for fd in (stdin_fd, stdout_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
