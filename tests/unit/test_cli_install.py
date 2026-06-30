# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Install/uninstall ergonomics: registering the MCP server in a client config
and the bundled-skills round-trip. No MCP client or network required."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshtastic_mcp import __main__ as cli


def _args(**kw):
    class A:
        pass

    a = A()
    defaults = {
        "client": "claude-code",
        "scope": "user",
        "config": None,
        "name": "meshtastic",
        "local": False,
        "env": None,
        "no_skills": True,
        "skills_dest": Path("/nonexistent"),
        "print": False,
        "dry_run": False,
        "purge_skills": False,
    }
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(a, k, v)
    return a


def test_install_adds_entry_preserving_other_keys(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"existing": {"command": "foo"}}, "keep": 1}))

    assert cli._install(_args(config=str(cfg), local=True)) == 0
    data = json.loads(cfg.read_text())
    # our server registered, existing server + unrelated keys preserved
    assert "meshtastic" in data["mcpServers"]
    assert data["mcpServers"]["existing"] == {"command": "foo"}
    assert data["keep"] == 1
    # --local registers the current interpreter via `-m meshtastic_mcp`
    assert data["mcpServers"]["meshtastic"]["args"] == ["-m", "meshtastic_mcp"]


def test_install_default_is_uvx_and_env_passthrough(tmp_path):
    cfg = tmp_path / "mcp.json"
    cli._install(_args(config=str(cfg), env=[("MESHTASTIC_FIRMWARE_ROOT", "/fw")]))
    entry = json.loads(cfg.read_text())["mcpServers"]["meshtastic"]
    assert entry["command"] == "uvx" and entry["args"] == ["meshtastic-mcp"]
    assert entry["env"] == {"MESHTASTIC_FIRMWARE_ROOT": "/fw"}


def test_install_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "mcp.json"
    cli._install(_args(config=str(cfg), dry_run=True))
    assert not cfg.exists()


def test_uninstall_removes_only_our_entry(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"meshtastic": {"command": "uvx"}, "other": {"command": "x"}}})
    )
    cli._uninstall(_args(config=str(cfg)))
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert "meshtastic" not in servers and "other" in servers


def test_install_refuses_invalid_json(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ not valid json ]")
    with pytest.raises(SystemExit):
        cli._install(_args(config=str(cfg)))


def test_skills_install_uninstall_roundtrip(tmp_path):
    assert cli._skills_install(tmp_path) == 0
    names = cli._bundled_skill_names()
    assert names and all((tmp_path / n).is_dir() for n in names)
    assert cli._skills_uninstall(tmp_path) == 0
    assert not any((tmp_path / n).exists() for n in names)


def test_client_config_paths_resolve():
    assert cli._client_config_path("claude-code", "user").name == ".claude.json"
    assert cli._client_config_path("claude-code", "project").name == ".mcp.json"
    assert cli._client_config_path("cursor", "user").name == "mcp.json"
