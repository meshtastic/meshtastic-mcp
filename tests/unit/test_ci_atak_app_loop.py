# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure-bits tests for the ATAK app-plane e2e helper (no emulator/ATAK needed).

The emulator orchestration (`run`) needs the android capability + ATAK-CIV and
is exercised by the opt-in CI job; here we cover the pure helpers and CLI
guards, mirroring test_ci_device_mesh_e2e.py.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from meshtastic_mcp.replay import tak

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
requires_tak = pytest.mark.skipif(not tak.available(), reason="[tak] extra not installed")


def _load(name: str = "ci_atak_app_loop"):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_file_exists():
    assert (_SCRIPTS / "ci_atak_app_loop.py").is_file()


def test_atak_stream_pref_encodes_connect_string():
    mod = _load()
    pref = mod.atak_stream_pref("10.0.2.2", 8087, name="meshsim")
    assert "cot_streams" in pref
    assert "10.0.2.2:8087:tcp" in pref  # ATAK host:port:proto connect string
    assert "enabled0" in pref and "meshsim" in pref


def test_main_exits_2_when_apk_missing():
    assert _load().main(["--atak-apk", "/definitely/not/here/ATAK.apk"]) == 2


def test_verdict_prefix_is_grepable():
    # the loop reuses the shared verdict() with an atak-render leg name
    mod = _load()
    line = mod._mesh.verdict("atak-render", True, "Coyote-1", 0)
    assert line.startswith("LOOP atak-render PASS")
    assert "token='Coyote-1'" in line


@requires_tak
def test_expected_callsigns_from_sim_squad():
    mod = _load()
    _cap, events = mod.build_squad(seed=8, nodes=120, days=1)
    callsigns = mod.expected_callsigns(events)
    assert len(callsigns) == 5  # team_nodes=5 in build_squad
    assert all(cs and "-" in cs for cs in callsigns)
