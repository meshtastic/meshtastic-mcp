# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for config snapshot diffing (pure logic, no hardware)."""

from __future__ import annotations

import json

import pytest

from meshtastic_mcp import config_snapshot


@pytest.fixture(autouse=True)
def _isolate_snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def _write_snapshot(name: str, config: dict) -> None:
    path = config_snapshot._snapshot_path(name)
    path.write_text(json.dumps({"name": name, "config": config}), encoding="utf-8")


def test_flatten_nested_config():
    flat = config_snapshot._flatten({"localConfig": {"lora": {"region": "US", "tx_power": 30}}})
    assert flat == {
        "localConfig.lora.region": "US",
        "localConfig.lora.tx_power": 30,
    }


def test_diff_detects_changed_field():
    _write_snapshot("before", {"localConfig": {"lora": {"region": "US"}}})
    _write_snapshot("after", {"localConfig": {"lora": {"region": "EU868"}}})
    result = config_snapshot.diff("before", "after")
    assert result["changed"] == {"localConfig.lora.region": {"from": "US", "to": "EU868"}}
    assert not result["added"]
    assert not result["removed"]
    assert result["identical"] is False


def test_diff_detects_added_and_removed():
    _write_snapshot("a", {"localConfig": {"lora": {"region": "US"}}})
    _write_snapshot("b", {"localConfig": {"device": {"role": "ROUTER"}}})
    result = config_snapshot.diff("a", "b")
    assert "localConfig.device.role" in result["added"]
    assert "localConfig.lora.region" in result["removed"]


def test_diff_identical_snapshots():
    cfg = {"localConfig": {"lora": {"region": "US"}}}
    _write_snapshot("x", cfg)
    _write_snapshot("y", cfg)
    result = config_snapshot.diff("x", "y")
    assert result["identical"] is True


def test_snapshot_name_rejects_traversal():
    with pytest.raises(ValueError, match="Invalid snapshot name"):
        config_snapshot._snapshot_path("../../etc/passwd")


def test_diff_missing_snapshot_raises():
    with pytest.raises(FileNotFoundError):
        config_snapshot.diff("does-not-exist", "also-missing")


def test_list_snapshots_returns_metadata():
    _write_snapshot("snap1", {"localConfig": {}})
    out = config_snapshot.list_snapshots()
    names = {s["name"] for s in out}
    assert "snap1" in names
