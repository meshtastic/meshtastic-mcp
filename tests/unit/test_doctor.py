# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the environment doctor."""

from __future__ import annotations

import pytest

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


# --- OCR backend: importable is not the same as usable ----------------------
#
# Live-discovered (2026-07-29): a torch built against a different NumPy ABI
# IMPORTS fine — NumPy only warns, printing a stack dump that reads like a crash
# — and then every numpy↔torch conversion raises "Numpy is not available". So
# `import easyocr` succeeding proved nothing, and doctor reported the OCR
# capability `ok` while the backend could not process a single image.


def _fake_easyocr_state(monkeypatch, *, usable: bool, why: str | None) -> None:
    monkeypatch.setattr(doctor, "_easyocr_state", lambda: (usable, why))


def _ocr(rep) -> object:
    return next(c for c in rep.checks if c.name == "ocr")


def test_ocr_broken_easyocr_is_not_reported_ok(monkeypatch) -> None:
    """No fallback + an installed-but-unusable easyocr => not ok, and the detail
    names the real cause instead of claiming nothing is installed."""
    _fake_easyocr_state(
        monkeypatch,
        usable=False,
        why="installed but its numpy↔torch bridge is broken: Numpy is not available",
    )
    monkeypatch.setattr(doctor, "_which", lambda name: None)  # no tesseract binary

    check = _ocr(doctor.run())

    assert check.status != doctor.STATUS_OK, "an unusable backend must not read as healthy"
    assert "numpy" in check.detail.lower()
    assert "no OCR backend importable" not in check.detail, "misleading: easyocr IS installed"
    assert check.fix, "a degraded capability still needs an actionable fix"
    assert "numpy" in check.fix.lower() or "torch" in check.fix.lower()


def test_ocr_broken_easyocr_falls_back_to_pytesseract(monkeypatch) -> None:
    """With a working pytesseract the capability is still ok — but the report
    says why the preferred backend was passed over."""
    _fake_easyocr_state(
        monkeypatch, usable=False, why="installed but its numpy↔torch bridge is broken: boom"
    )
    monkeypatch.setattr(doctor, "_which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", object())

    check = _ocr(doctor.run())

    assert check.status == doctor.STATUS_OK
    assert "pytesseract" in check.detail
    assert "easyocr" in check.detail, "should explain why easyocr was skipped"


def test_ocr_absent_easyocr_still_reports_plain_missing(monkeypatch) -> None:
    """The pre-existing message must survive for a genuinely empty environment —
    'bridge broken' advice would be nonsense when nothing is installed."""
    _fake_easyocr_state(monkeypatch, usable=False, why="not importable (ModuleNotFoundError)")
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    monkeypatch.delitem(__import__("sys").modules, "pytesseract", raising=False)

    check = _ocr(doctor.run())

    assert check.status == doctor.STATUS_MISSING
    assert check.detail == "no OCR backend importable"


def test_run_survives_a_probe_that_raises(monkeypatch) -> None:
    """run() documents "never raises", but probes were called inline while
    building the list, so one blowing up discarded every other result and printed
    a traceback — the opposite of what you need from the tool you were told to
    run when a dependency misbehaves."""

    def boom() -> doctor.Check:
        raise RuntimeError("Numpy is not available")

    monkeypatch.setattr(doctor, "_ocr_check", boom)

    rep = doctor.run()  # must not raise

    assert len(rep.checks) > 5, "one bad probe must not discard the other checks"
    check = _ocr(rep)
    assert check.status == doctor.STATUS_DEGRADED
    assert "RuntimeError" in check.detail and "Numpy is not available" in check.detail
    assert check.fix, "even a probe failure tells the caller what to do next"
    # A failed probe means "unknown", not "absent" — it must not be advertised as
    # a missing dependency the caller can install their way out of.
    assert "ocr" not in [c.name for c in rep.missing]


def test_ctrl_c_still_aborts_a_doctor_run(monkeypatch) -> None:
    """The isolation above catches Exception, deliberately NOT BaseException:
    doctor shells out to slow subprocesses (pio, gh, xcrun) and Ctrl-C has to
    keep working. Swallowing KeyboardInterrupt would make it uninterruptible."""

    def interrupted() -> doctor.Check:
        raise KeyboardInterrupt

    monkeypatch.setattr(doctor, "_ocr_check", interrupted)

    with pytest.raises(KeyboardInterrupt):
        doctor.run()
