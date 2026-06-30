# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the Kotlin-SDK CLI device-IO bridge (`sdk_cli`).

The real `cli` is a JVM binary; these exercise the bridge against the documented
NDJSON envelope contract using a tiny fake launcher, so they're deterministic and
need neither a JVM nor a device.
"""

from __future__ import annotations

import os
import stat
import textwrap

import pytest

from meshtastic_mcp import capabilities, sdk_cli


def test_normalize_transport_forms():
    assert sdk_cli.normalize_transport("tcp:host:4403") == "tcp:host:4403"
    assert sdk_cli.normalize_transport("serial:/dev/ttyUSB0") == "serial:/dev/ttyUSB0"
    assert sdk_cli.normalize_transport("ble:Trav") == "ble:Trav"
    assert sdk_cli.normalize_transport("tcp://meshtastic.local") == "tcp:meshtastic.local"
    assert sdk_cli.normalize_transport("/dev/ttyUSB1") == "serial:/dev/ttyUSB1"
    with pytest.raises(sdk_cli.SdkCliError):
        sdk_cli.normalize_transport("wat")


def test_parse_envelopes_skips_junk():
    stdout = textwrap.dedent(
        """\
        not json
        {"type":"info","ts":1,"data":{"nodeCount":3}}

        {"type":"done","ts":2,"data":{"reason":"ok","exit":0}}
        """
    )
    envs = sdk_cli.parse_envelopes(stdout)
    assert [e["type"] for e in envs] == ["info", "done"]


def test_available_mirrors_path(monkeypatch):
    monkeypatch.delenv(sdk_cli.CLI_ENV, raising=False)
    monkeypatch.delenv(sdk_cli.ROOT_ENV, raising=False)
    monkeypatch.setattr(sdk_cli.shutil, "which", lambda _: None)
    assert sdk_cli.available() is False
    assert capabilities.has_sdk_cli() is False


def _write_fake_cli(tmp_path, body: str) -> str:
    p = tmp_path / "cli"
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def test_run_success_against_fake_cli(tmp_path, monkeypatch):
    # Fake launcher: assert it received --json/--transport, emit an info + done envelope.
    cli = _write_fake_cli(
        tmp_path,
        textwrap.dedent(
            """\
            echo "$@" >&2
            echo '{"type":"info","ts":1,"data":{"transport":"tcp host:4403","nodeCount":2}}'
            echo '{"type":"done","ts":2,"data":{"reason":"ok","exit":0}}'
            """
        ),
    )
    monkeypatch.setenv(sdk_cli.CLI_ENV, cli)
    assert sdk_cli.available() is True

    res = sdk_cli.device_info("tcp://host:4403")
    assert res["ok"] is True
    assert res["exit"] == 0
    assert res["info"]["nodeCount"] == 2
    assert res["error"] is None
    # The normalized transport flag reached the launcher (echoed to stderr).
    assert "tcp:host:4403" in (res["stderr"] or "")
    assert "--json" in (res["stderr"] or "")


def test_run_error_envelope_is_not_ok(tmp_path, monkeypatch):
    cli = _write_fake_cli(
        tmp_path,
        textwrap.dedent(
            """\
            echo '{"type":"error","ts":1,"data":{"code":"timeout","message":"handshake"}}'
            echo '{"type":"done","ts":2,"data":{"reason":"timeout","exit":3}}'
            exit 3
            """
        ),
    )
    monkeypatch.setenv(sdk_cli.CLI_ENV, cli)
    res = sdk_cli.list_nodes("serial:/dev/ttyUSB0")
    assert res["ok"] is False
    assert res["exit"] == 3
    assert res["error"]["code"] == "timeout"
    assert res["nodes"] == []


def test_run_missing_cli_raises(monkeypatch):
    monkeypatch.delenv(sdk_cli.CLI_ENV, raising=False)
    monkeypatch.delenv(sdk_cli.ROOT_ENV, raising=False)
    monkeypatch.setattr(sdk_cli.shutil, "which", lambda _: None)
    with pytest.raises(sdk_cli.SdkCliError):
        sdk_cli.run(["info"], "tcp:host")


def test_root_env_resolution(tmp_path, monkeypatch):
    base = tmp_path / "samples" / "cli" / "build" / "install" / "cli" / "bin"
    base.mkdir(parents=True)
    launcher = base / "cli"
    launcher.write_text("#!/usr/bin/env bash\n:")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    monkeypatch.delenv(sdk_cli.CLI_ENV, raising=False)
    monkeypatch.setenv(sdk_cli.ROOT_ENV, str(tmp_path))
    assert sdk_cli.cli_path() == str(launcher)
    assert os.path.isfile(sdk_cli.cli_path())
