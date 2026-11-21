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


# ---------- the server itself ----------------------------------------------


def _serve(monkeypatch, info, *, ours="1.0.0", answers=True) -> doctor.Check:
    from meshtastic_mcp import bridge

    monkeypatch.setattr(bridge, "find_local", lambda: info)
    monkeypatch.setattr(doctor, "_installed_version", lambda: ours)
    monkeypatch.setattr(doctor, "_answers", lambda url: answers)
    monkeypatch.delenv("MESHTASTIC_WORKSPACE", raising=False)
    return doctor._serve_version_check()


def test_a_serve_running_other_code_is_degraded_with_both_versions(monkeypatch) -> None:
    info = {"url": "http://127.0.0.1:8765/mcp", "pid": 42, "version": "0.4.1.dev42+g19d990745"}
    c = _serve(monkeypatch, info)
    assert c.status == doctor.STATUS_DEGRADED
    assert "0.4.1.dev42+g19d990745" in c.detail and "1.0.0" in c.detail
    assert "restart" in c.fix


def test_a_serve_running_this_code_is_ok(monkeypatch) -> None:
    info = {"url": "http://127.0.0.1:8765/mcp", "pid": 42, "version": "1.0.0"}
    assert _serve(monkeypatch, info).status == doctor.STATUS_OK


def test_no_serve_is_ok(monkeypatch) -> None:
    assert _serve(monkeypatch, None).status == doctor.STATUS_OK


def test_a_serve_that_does_not_answer_is_reported_not_fatal(monkeypatch) -> None:
    info = {"url": "http://127.0.0.1:8765/mcp", "pid": 42, "version": "1.0.0"}
    c = _serve(monkeypatch, info, answers=False)
    assert c.status == doctor.STATUS_DEGRADED
    assert "does not answer" in c.detail


def _workspace(monkeypatch, tmp_path):
    checkout = tmp_path / "meshtastic-mcp"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setenv("MESHTASTIC_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(doctor, "_git_head", lambda repo: "274e22b7e0123456789")
    return checkout


def test_a_git_install_of_other_code_than_the_checkout_is_degraded(monkeypatch, tmp_path) -> None:
    checkout = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_install_source", lambda: ("git https://x@19d990745", False))
    monkeypatch.setattr(doctor, "_installed_version", lambda: "0.4.1.dev42+g19d990745")
    c = doctor._install_source_check()
    assert c is not None and c.status == doctor.STATUS_DEGRADED
    assert "274e22b7e" in c.detail
    assert f"--editable '{checkout}[ui,tasks]'" in c.fix


def test_an_install_of_the_checkout_head_is_ok(monkeypatch, tmp_path) -> None:
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_install_source", lambda: ("git https://x@274e22b7e", False))
    monkeypatch.setattr(doctor, "_installed_version", lambda: "0.4.1.dev62+g274e22b7e")
    c = doctor._install_source_check()
    assert c is not None and c.status == doctor.STATUS_OK


def test_an_editable_install_is_ok(monkeypatch, tmp_path) -> None:
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_install_source", lambda: ("editable /somewhere", True))
    c = doctor._install_source_check()
    assert c is not None and c.status == doctor.STATUS_OK


def test_no_checkout_means_no_install_source_check(monkeypatch) -> None:
    monkeypatch.delenv("MESHTASTIC_WORKSPACE", raising=False)
    assert doctor._install_source_check() is None


def _exe(path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def test_a_binary_off_the_servers_path_is_degraded_not_missing(monkeypatch, tmp_path) -> None:
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    _exe(sbin / "uhubctl")
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    monkeypatch.setattr(doctor, "_EXTRA_BIN_DIRS", (str(sbin),))
    monkeypatch.setattr(doctor, "_login_path_dirs", lambda: ())
    checks = [doctor.Check("uhubctl", "observability", doctor.STATUS_MISSING, "x", fix="apt")]
    doctor._apply_path_gaps(checks)
    c = checks[0]
    assert c.status == doctor.STATUS_DEGRADED
    assert c.detail == f"found at {sbin / 'uhubctl'} but not on this server's PATH"
    assert str(sbin) in c.fix and "Environment=PATH" in c.fix


def test_a_binary_on_the_login_shells_path_is_found(monkeypatch, tmp_path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "pio")
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    monkeypatch.setattr(doctor, "_EXTRA_BIN_DIRS", ())
    monkeypatch.setattr(doctor, "_login_path_dirs", lambda: (str(bindir),))
    checks = [doctor.Check("platformio", "firmware", doctor.STATUS_MISSING, "x", fix="pipx")]
    doctor._apply_path_gaps(checks)
    assert checks[0].status == doctor.STATUS_DEGRADED


def test_a_binary_absent_everywhere_stays_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    monkeypatch.setattr(doctor, "_EXTRA_BIN_DIRS", (str(tmp_path),))
    monkeypatch.setattr(doctor, "_login_path_dirs", lambda: ())
    checks = [doctor.Check("uhubctl", "observability", doctor.STATUS_MISSING, "x", fix="apt")]
    doctor._apply_path_gaps(checks)
    assert checks[0].status == doctor.STATUS_MISSING and checks[0].fix == "apt"
