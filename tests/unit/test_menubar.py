# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the macOS menu-bar controller's pure helpers.

``menubar`` imports only stdlib at module scope (rumps is lazy-imported inside
``main``), so these run on any platform without the ``[menubar]`` extra.
"""

from __future__ import annotations

from meshtastic_mcp import menubar


def test_parse_running_with_pid():
    out = "\tstate = running\n\tpid = 4242\n\tprogram = /bin/bash\n"
    assert menubar.parse_service_state(out) == (True, 4242)


def test_parse_loaded_but_idle_has_no_pid():
    assert menubar.parse_service_state("\tstate = waiting\n") == (False, None)


def test_parse_spawn_scheduled_is_not_running():
    # e.g. moments after a crash, before KeepAlive respawns it
    assert menubar.parse_service_state("\tstate = spawn scheduled\n") == (False, None)


def test_parse_empty_output():
    assert menubar.parse_service_state("") == (False, None)


def test_status_line_running(monkeypatch):
    monkeypatch.setattr(menubar, "service_pid", lambda: 4242)
    monkeypatch.setattr(menubar, "http_healthy", lambda: True)
    glyph, line = menubar.status_line()
    assert glyph == menubar.GLYPH_RUNNING
    assert "4242" in line


def test_status_line_pending_when_up_but_not_answering(monkeypatch):
    monkeypatch.setattr(menubar, "service_pid", lambda: 99)
    monkeypatch.setattr(menubar, "http_healthy", lambda: False)
    glyph, _line = menubar.status_line()
    assert glyph == menubar.GLYPH_PENDING


def test_status_line_stopped_when_no_pid(monkeypatch):
    monkeypatch.setattr(menubar, "service_pid", lambda: None)
    glyph, line = menubar.status_line()
    assert glyph == menubar.GLYPH_STOPPED
    assert "stopped" in line
