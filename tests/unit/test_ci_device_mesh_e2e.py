# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the CI e2e helpers (pure bits only — no mesh/emulator/simulator required)."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str = "ci_device_mesh_e2e"):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_files_exist() -> None:
    for name in ("ci_device_mesh_e2e", "ci_android_app_loop", "ci_apple_app_loop"):
        assert (_SCRIPTS / f"{name}.py").is_file()


def test_verdict_pass_includes_latency_and_grepable_prefix() -> None:
    mod = _load()
    line = mod.verdict("inbound", True, "CI-E2E-1", 1234, extra="{'from': '!aa'}")
    assert line.startswith("LOOP inbound PASS")
    assert "token='CI-E2E-1'" in line
    assert "latency=1234ms" in line


def test_verdict_fail_omits_latency() -> None:
    mod = _load()
    line = mod.verdict("inbound", False, "CI-E2E-2", None)
    assert line.startswith("LOOP inbound FAIL")
    assert "latency=" not in line


def test_mesh_up_is_exposed_for_reuse() -> None:
    # Both app-loop helpers import this shared context manager.
    assert hasattr(_load(), "mesh_up")


def test_device_main_exits_2_when_binary_missing() -> None:
    assert _load().main(["--binary", "/definitely/not/here/meshtasticd"]) == 2


def test_device_main_requires_binary_arg() -> None:
    with pytest.raises(SystemExit):
        _load().main([])


@pytest.mark.parametrize("helper", ["ci_android_app_loop", "ci_apple_app_loop"])
def test_app_loop_helpers_import_and_validate_inputs(helper: str) -> None:
    mod = _load(helper)
    # missing binary -> exit 2 (both take --binary plus an app/apk arg)
    extra = ["--apk", "/nope"] if helper == "ci_android_app_loop" else ["--app", "/nope"]
    assert mod.main(["--binary", "/definitely/not/here", *extra]) == 2
