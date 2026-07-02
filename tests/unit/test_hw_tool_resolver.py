# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`config._hw_tool` resolution order — including the PlatformIO fallback that
fixes the esp32s3 bake (esptool ships with PlatformIO; a bare checkout has no
separate install)."""

from __future__ import annotations

import pytest

from meshtastic_mcp import config as cfg


def _exe(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_env_var_wins(monkeypatch, tmp_path):
    tool = _exe(tmp_path / "esptool")
    monkeypatch.setenv("MESHTASTIC_ESPTOOL_BIN", str(tool))
    assert cfg.esptool_bin() == tool


def test_env_var_must_be_executable(monkeypatch, tmp_path):
    bad = tmp_path / "nope"
    bad.write_text("x")  # not +x
    monkeypatch.setenv("MESHTASTIC_ESPTOOL_BIN", str(bad))
    with pytest.raises(cfg.ConfigError):
        cfg.esptool_bin()


def test_interpreter_bin_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("MESHTASTIC_ESPTOOL_BIN", raising=False)
    monkeypatch.setattr(cfg, "firmware_root", lambda: tmp_path / "no_repo")
    monkeypatch.setattr(cfg.shutil, "which", lambda _n: None)
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.setattr(cfg.sys, "executable", str(bindir / "python"))
    tool = _exe(bindir / "esptool")
    assert cfg.esptool_bin() == tool


def test_platformio_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("MESHTASTIC_ESPTOOL_BIN", raising=False)
    monkeypatch.setattr(cfg, "firmware_root", lambda: tmp_path / "no_repo")
    monkeypatch.setattr(cfg.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cfg.sys, "executable", str(tmp_path / "empty" / "python"))
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    pkg = home / ".platformio" / "packages" / "tool-esptoolpy"
    pkg.mkdir(parents=True)
    tool = _exe(pkg / "esptool.py")
    assert cfg.esptool_bin() == tool


def test_not_found_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("MESHTASTIC_ESPTOOL_BIN", raising=False)
    monkeypatch.setattr(cfg, "firmware_root", lambda: tmp_path / "no_repo")
    monkeypatch.setattr(cfg.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cfg.sys, "executable", str(tmp_path / "empty" / "python"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    with pytest.raises(cfg.ConfigError, match="esptool"):
        cfg.esptool_bin()
