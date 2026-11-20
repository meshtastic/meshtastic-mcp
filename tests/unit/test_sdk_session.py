# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""The SDK backend's long-lived `cli mcp` session: one connect, then reads on it."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from meshtastic_mcp import backends, sdk_cli
from meshtastic_mcp.backends import sdk_session
from meshtastic_mcp.tools import core

FAKE = Path(__file__).parent / "data" / "fake_sdk_cli.py"


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    launcher = tmp_path / "cli"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_SDK_LOG", str(log))
    monkeypatch.setenv(sdk_cli.CLI_ENV, str(launcher))
    monkeypatch.setenv(backends.BACKEND_ENV, "sdk")
    monkeypatch.setattr(sdk_session, "_session", None)
    monkeypatch.setattr(sdk_session, "_unavailable", None)
    return log


def test_reads_share_one_connection(fake_cli) -> None:
    first = core.inspect(node="ble:Heltec", what=["summary", "nodes"])
    second = core.inspect(node="ble:Heltec", what="nodes")
    assert first["backend"] == "sdk"
    assert first["summary"]["node_id"] == "!3a14aa13" and first["summary"]["num_nodes"] == 2
    assert [n["node_id"] for n in second["nodes"]] == ["!3a14aa13", "!c2c91376"]
    assert fake_cli.read_text().splitlines() == ["connect ble:Heltec"]


def test_a_new_target_connects_again(fake_cli) -> None:
    core.inspect(node="ble:Heltec")
    core.inspect(node="tcp://bench:4403")
    assert fake_cli.read_text().splitlines() == ["connect ble:Heltec", "connect tcp:bench:4403"]


def test_a_cli_without_mcp_falls_back_to_one_shot_runs(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "cli"
    launcher.write_text("#!/bin/sh\nexit 2\n")
    launcher.chmod(0o755)
    monkeypatch.setenv(sdk_cli.CLI_ENV, str(launcher))
    monkeypatch.setattr(sdk_session, "_session", None)
    monkeypatch.setattr(sdk_session, "_unavailable", None)
    assert sdk_session.session(str(launcher)) is None
    assert sdk_session._unavailable
    assert os.access(launcher, os.X_OK)
