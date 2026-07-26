"""Doctor coverage for the native `meshtasticd` build prerequisites.

Building real firmware for the host is what makes the hardware-free virtual-radio path possible. On
macOS it needs four Homebrew formulae; a missing one only surfaces minutes into a PlatformIO build as
a bare `fatal error: 'argp.h' file not found`, which is exactly the kind of dead end `doctor` exists
to pre-empt.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp import doctor


def test_reports_missing_formulae_with_an_actionable_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(
        doctor,
        "_MESHTASTICD_DEPS_MAC",
        (("argp-standalone", "/nope/argp.h"), ("yaml-cpp", "/nope/yaml.h")),
    )
    check = doctor._meshtasticd_build_check()

    assert check.status == doctor.STATUS_MISSING
    assert check.group == "firmware"
    # The fix must be the command to run, not a description of the problem.
    assert check.fix == "brew install argp-standalone yaml-cpp"
    assert "argp-standalone" in check.detail


def test_reports_ok_when_every_header_is_present(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    header = tmp_path / "argp.h"
    header.write_text("/* present */")
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_MESHTASTICD_DEPS_MAC", (("argp-standalone", str(header)),))

    check = doctor._meshtasticd_build_check()
    assert check.status == doctor.STATUS_OK
    assert check.fix == ""


def test_only_missing_formulae_appear_in_the_fix(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    present = tmp_path / "uv.h"
    present.write_text("/* present */")
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(
        doctor,
        "_MESHTASTICD_DEPS_MAC",
        (("libuv", str(present)), ("yaml-cpp", "/nope/yaml.h")),
    )

    check = doctor._meshtasticd_build_check()
    assert check.fix == "brew install yaml-cpp"
    assert "libuv" not in check.fix


def test_non_mac_reports_ok_but_still_names_the_debian_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Linux CI installs these via apt; the check should not fail there, but the report should still
    # say what is needed so the hint is discoverable from either platform.
    monkeypatch.setattr(doctor, "_IS_MAC", False)
    check = doctor._meshtasticd_build_check()

    assert check.status == doctor.STATUS_OK
    assert "libyaml-cpp-dev" in check.detail


def test_check_is_registered_in_the_report() -> None:
    report = doctor.run()
    assert any(c.name == "meshtasticd-build-deps" for c in report.checks)
