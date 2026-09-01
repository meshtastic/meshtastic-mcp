"""Boot-capture support in serial sessions, and the empty-read hint.

Both exist because of the same failure: a monitor that returns nothing looks identical whether the
device is dead, the tool is broken, or the firmware is simply logging somewhere else. During the
BLE-mesh work that ambiguity cost hours - the node was healthy and logging happily over the
StreamAPI, with the console silent because `security.debug_log_api_enabled` was set.
"""

from __future__ import annotations

from typing import Any

import pytest

from meshtastic_mcp import serial_session


class _FakeProc:
    """Stands in for the `pio device monitor` subprocess."""

    def __init__(self, running: bool = True) -> None:
        self._running = running

    def poll(self) -> int | None:
        return None if self._running else 0


def _session(*, lines: list[str] | None = None, running: bool = True) -> Any:
    s = serial_session.SerialSession(
        id="test",
        port="/dev/null",
        baud=115200,
        filters=[],
        env=None,
        proc=_FakeProc(running),  # type: ignore[arg-type]
    )
    for line in lines or []:
        s.buffer.append(line)
        s.total_lines += 1
    return s


def test_empty_read_explains_the_silent_console() -> None:
    result = serial_session.read_session(_session())

    assert result["lines"] == []
    # The point of the hint is naming debug_log_api, which is the cause a caller will not guess.
    assert "debug_log_api" in result["hint"]
    assert "reset=True" in result["hint"]


def test_no_hint_once_the_port_has_spoken() -> None:
    result = serial_session.read_session(_session(lines=["INFO | boot"]))

    assert result["lines"] == ["INFO | boot"]
    assert "hint" not in result, "a hint after real output would just be noise"


def test_no_hint_when_the_monitor_has_exited() -> None:
    # eof is its own signal and already reported; a hint about silent consoles would mislead.
    result = serial_session.read_session(_session(running=False))

    assert result["eof"] is True
    assert "hint" not in result


def test_pulse_reset_reports_failure_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unopenable port must not take the session down with it: the caller should still get a
    # monitor, just one that starts mid-run instead of at boot.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such port")

    monkeypatch.setattr(serial_session.serial, "Serial", _boom)

    assert serial_session.pulse_reset("/dev/does-not-exist") is False


def test_pulse_reset_drives_the_auto_reset_lines_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPIO0 must stay high throughout, or the chip enters the ROM bootloader instead of booting."""
    calls: list[tuple[str, bool]] = []

    class _FakeSerial:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeSerial:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def setDTR(self, value: bool) -> None:
            calls.append(("dtr", value))

        def setRTS(self, value: bool) -> None:
            calls.append(("rts", value))

    monkeypatch.setattr(serial_session.serial, "Serial", _FakeSerial)

    assert serial_session.pulse_reset("/dev/fake") is True
    assert calls == [("dtr", False), ("rts", True), ("rts", False)]


def test_session_summary_reports_whether_a_reset_happened() -> None:
    s = _session()
    assert serial_session.session_summary(s)["reset_pulsed"] is None, "no reset requested"

    s.reset_pulsed = True
    assert serial_session.session_summary(s)["reset_pulsed"] is True
