# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the environment doctor."""

from __future__ import annotations

import meshtastic_mcp.doctor as doctor


def test_run_never_raises_and_is_structured() -> None:
    rep = doctor.run()
    d = rep.to_dict()
    assert set(d) >= {"platform", "capabilities", "ok", "checks", "missing", "fix_commands"}
    assert isinstance(d["checks"], list) and d["checks"], "expected at least one probed check"
    # every check carries the required ergonomic fields
    for c in d["checks"]:
        assert set(c) >= {"name", "group", "status", "needed_for"}
        assert c["status"] in {doctor.STATUS_OK, doctor.STATUS_MISSING, doctor.STATUS_DEGRADED}


def test_missing_deps_carry_an_acquisition_command() -> None:
    rep = doctor.run()
    for c in rep.checks:
        if c.status == doctor.STATUS_MISSING:
            assert c.fix, f"missing dep {c.name!r} must tell the caller how to acquire it"


def test_idb_companion_points_at_the_facebook_tap_not_the_cask() -> None:
    # Regression guard for the live-discovered gotcha: the `companion` cask is the wrong thing.
    rep = doctor.run()
    idb = next(c for c in rep.checks if c.name == "idb_companion")
    if not idb.ok:
        assert "facebook/fb" in idb.fix
        assert "--cask companion" not in idb.fix


def test_fbidb_hint_pins_python_312() -> None:
    rep = doctor.run()
    fbidb = next(c for c in rep.checks if c.name == "fb-idb")
    if not fbidb.ok:
        assert "3.12" in fbidb.fix


def test_sdk_cli_check_present_and_actionable(monkeypatch) -> None:
    # With no launcher resolvable, the sdk-cli check must be MISSING and tell the
    # caller how to build/point at the meshtastic-sdk sample CLI.
    import meshtastic_mcp.sdk_cli as sdk_cli

    monkeypatch.delenv(sdk_cli.CLI_ENV, raising=False)
    monkeypatch.delenv(sdk_cli.ROOT_ENV, raising=False)
    monkeypatch.setattr(sdk_cli.shutil, "which", lambda _: None)
    rep = doctor.run()
    sdk = next(c for c in rep.checks if c.name == "sdk-cli")
    assert sdk.status == doctor.STATUS_MISSING
    assert "installDist" in sdk.fix
    assert sdk.env_override == sdk_cli.CLI_ENV


def test_report_renders_text() -> None:
    text = doctor.report()
    assert "meshtastic-mcp doctor" in text
    assert "capabilities:" in text
