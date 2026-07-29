# SPDX-FileCopyrightText: Meshtastic MCP contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Doctor coverage for the native `meshtasticd` build prerequisites.

Building real firmware for the host is what makes the hardware-free virtual-radio path possible. On
macOS it needs four Homebrew formulae; a missing one only surfaces minutes into a PlatformIO build as
a bare `fatal error: 'argp.h' file not found`, which is exactly the kind of dead end `doctor` exists
to pre-empt.

Both platforms are probed. The macOS paths are resolved against Homebrew's actual prefix rather than
a hard-coded one, so these tests pin a fake prefix instead of relying on the host's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshtastic_mcp import doctor


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("/* present */")
    return path


# --- macOS -----------------------------------------------------------------


def test_reports_missing_formulae_with_an_actionable_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_brew_prefix", lambda: tmp_path)
    monkeypatch.setattr(
        doctor,
        "_MESHTASTICD_DEPS_MAC",
        (("argp-standalone", "include/argp.h"), ("yaml-cpp", "include/yaml.h")),
    )
    check = doctor._meshtasticd_build_check()

    assert check.status == doctor.STATUS_MISSING
    assert check.group == "firmware"
    # The fix must be the command to run, not a description of the problem.
    assert check.fix == "brew install argp-standalone yaml-cpp"
    assert "argp-standalone" in check.detail


def test_reports_ok_when_every_header_is_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _touch(tmp_path / "include/argp.h")
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_brew_prefix", lambda: tmp_path)
    monkeypatch.setattr(doctor, "_MESHTASTICD_DEPS_MAC", (("argp-standalone", "include/argp.h"),))

    check = doctor._meshtasticd_build_check()
    assert check.status == doctor.STATUS_OK
    assert check.fix == ""


def test_only_missing_formulae_appear_in_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _touch(tmp_path / "include/uv.h")
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_brew_prefix", lambda: tmp_path)
    monkeypatch.setattr(
        doctor,
        "_MESHTASTICD_DEPS_MAC",
        (("libuv", "include/uv.h"), ("yaml-cpp", "include/yaml.h")),
    )

    check = doctor._meshtasticd_build_check()
    assert check.fix == "brew install yaml-cpp"
    assert "libuv" not in check.fix


def test_headers_resolve_against_the_real_prefix_not_a_hard_coded_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Intel Macs put Homebrew at /usr/local, Apple silicon at /opt/homebrew.

    A hard-coded prefix reported every formula missing on Intel even when all were installed, so
    the paths are prefix-relative and joined onto whatever `brew --prefix` reports.
    """
    intel_like = tmp_path / "usr/local"
    _touch(intel_like / "include/uv.h")
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_brew_prefix", lambda: intel_like)
    monkeypatch.setattr(doctor, "_MESHTASTICD_DEPS_MAC", (("libuv", "include/uv.h"),))

    assert doctor._meshtasticd_build_check().status == doctor.STATUS_OK


def test_missing_homebrew_is_reported_rather_than_silently_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_IS_MAC", True)
    monkeypatch.setattr(doctor, "_brew_prefix", lambda: None)

    check = doctor._meshtasticd_build_check()
    assert check.status == doctor.STATUS_MISSING
    assert "Homebrew" in check.detail
    assert "install.sh" in check.fix


# --- Linux -----------------------------------------------------------------


def test_linux_missing_packages_are_probed_not_assumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linux used to return ok unconditionally, which made this a check in name only.

    A Debian user missing a package got a green tick and then the same missing-header build
    failure the check exists to pre-empt.
    """
    monkeypatch.setattr(doctor, "_IS_MAC", False)
    monkeypatch.setattr(
        doctor,
        "_MESHTASTICD_DEPS_DEBIAN",
        (("libyaml-cpp-dev", str(tmp_path / "nope/yaml.h")),),
    )

    check = doctor._meshtasticd_build_check()
    assert check.status == doctor.STATUS_MISSING
    assert check.fix == "sudo apt install libyaml-cpp-dev"
    assert "native`" in check.detail or "-e native" in check.detail


def test_linux_reports_ok_when_headers_are_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    header = _touch(tmp_path / "usr/include/uv.h")
    monkeypatch.setattr(doctor, "_IS_MAC", False)
    monkeypatch.setattr(doctor, "_MESHTASTICD_DEPS_DEBIAN", (("libuv1-dev", str(header)),))

    check = doctor._meshtasticd_build_check()
    assert check.status == doctor.STATUS_OK
    assert check.fix == ""


# --- wiring ----------------------------------------------------------------


def test_check_is_registered_in_the_report() -> None:
    report = doctor.run()
    assert any(c.name == "meshtasticd-build-deps" for c in report.checks)
