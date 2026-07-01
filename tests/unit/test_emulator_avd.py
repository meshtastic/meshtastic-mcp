# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Portable unit tests for the emulator AVD wrapper (no emulator/hardware needed)."""

from __future__ import annotations

import subprocess

import pytest

from meshtastic_mcp.emulator import avd


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_tcp_dut_address_uses_host_alias() -> None:
    assert avd.tcp_dut_address(4403) == "10.0.2.2:4403"
    assert avd.tcp_dut_address(4404) == "10.0.2.2:4404"
    assert avd.EMULATOR_HOST_ALIAS == "10.0.2.2"


def test_first_emulator_serial_parses_adb_devices(monkeypatch) -> None:
    out = "List of devices attached\nemulator-5554\tdevice\n127.0.0.1:6555\tdevice\n"
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp(out))
    assert avd.first_emulator_serial() == "emulator-5554"


def test_first_emulator_serial_none_when_no_emulator(monkeypatch) -> None:
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp("List of devices attached\n"))
    assert avd.first_emulator_serial() is None


def test_is_app_installed(monkeypatch) -> None:
    pkgs = "package:com.android.shell\npackage:com.geeksville.mesh\n"
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp(pkgs))
    assert avd.is_app_installed("com.geeksville.mesh") is True
    assert avd.is_app_installed("com.example.absent") is False


def test_ui_dump_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(
        avd,
        "android",
        lambda *a, **k: _cp('[{"text": "Nodes 2/2", "center": "[100,200]"}]'),
    )
    els = avd.ui_dump()
    assert els[0]["text"] == "Nodes 2/2"


def test_find_text(monkeypatch) -> None:
    monkeypatch.setattr(avd, "android", lambda *a, **k: _cp('[{"text": "E2E-123"}]'))
    assert avd.find_text("E2E-123") is True
    assert avd.find_text("nope") is False


# ---------------------------------------------------------------------------
# Fresh-install launch (meshtastic/Meshtastic-Android#6044, skip_onboarding)
# ---------------------------------------------------------------------------
def test_grant_runtime_permissions_grants_full_set(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        avd, "resolve_package", lambda serial=None: "com.geeksville.mesh.fdroid.debug"
    )
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.grant_runtime_permissions()
    granted = [a[-1] for a in calls]
    assert granted == list(avd.ONBOARDING_PERMISSIONS)
    assert all(a[0:3] == ("shell", "pm", "grant") for a in calls)
    # package is the 4th token: pm grant <pkg> <perm>
    assert all(a[3] == "com.geeksville.mesh.fdroid.debug" for a in calls)


def test_grant_runtime_permissions_noop_when_no_package(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(avd, "resolve_package", lambda serial=None: None)
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.grant_runtime_permissions()
    assert calls == []  # nothing to grant against


def test_launch_app_with_skip_onboarding_adds_extra(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        avd, "resolve_package", lambda serial=None: "com.geeksville.mesh.fdroid.debug"
    )
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.launch_app(skip_onboarding=True)
    assert len(calls) == 1
    args = calls[0]
    assert args[0:4] == ("shell", "am", "start", "-n")
    assert args[4] == f"com.geeksville.mesh.fdroid.debug/{avd.MAIN_ACTIVITY}"
    assert "--ez" in args
    assert args[-2:] == (avd.EXTRA_SKIP_ONBOARDING, "true")


def test_launch_app_without_skip_omits_extra(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        avd, "resolve_package", lambda serial=None: "com.geeksville.mesh.fdroid.debug"
    )
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.launch_app()
    assert avd.EXTRA_SKIP_ONBOARDING not in calls[0]


def test_launch_app_raises_when_no_package(monkeypatch) -> None:
    monkeypatch.setattr(avd, "resolve_package", lambda serial=None: None)
    with pytest.raises(avd.EmulatorError):
        avd.launch_app()


def test_prepare_fresh_install_grants_then_launches(monkeypatch) -> None:
    order = []
    monkeypatch.setattr(
        avd, "grant_runtime_permissions", lambda pkg=None, serial=None: order.append("grant")
    )
    monkeypatch.setattr(
        avd,
        "launch_app",
        lambda pkg=None, serial=None, skip_onboarding=False, **k: order.append(
            ("launch", skip_onboarding)
        ),
    )
    avd.prepare_fresh_install()
    assert order == ["grant", ("launch", True)]  # permissions first, then skip-onboarding launch


# ---------------------------------------------------------------------------
# Deep links (meshtastic/Meshtastic-Android#6036, connect-by-address)
# ---------------------------------------------------------------------------
def test_resolve_package_prefers_fdroid_debug(monkeypatch) -> None:
    installed = {"com.geeksville.mesh.fdroid.debug", "com.geeksville.mesh"}
    monkeypatch.setattr(avd, "is_app_installed", lambda pkg, serial=None: pkg in installed)
    assert avd.resolve_package() == "com.geeksville.mesh.fdroid.debug"


def test_resolve_package_none_when_nothing_installed(monkeypatch) -> None:
    monkeypatch.setattr(avd, "is_app_installed", lambda pkg, serial=None: False)
    assert avd.resolve_package() is None


def test_deeplink_fires_am_start_with_resolved_package(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        avd, "resolve_package", lambda serial=None: "com.geeksville.mesh.fdroid.debug"
    )
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.deeplink("connections?address=t192.168.1.1:4403")
    assert len(calls) == 1
    args = calls[0]
    assert args[0:4] == ("shell", "am", "start", "-a")
    assert "android.intent.action.VIEW" in args
    assert any(a == "meshtastic://meshtastic/connections?address=t192.168.1.1:4403" for a in args)
    assert args[-1] == "com.geeksville.mesh.fdroid.debug"  # explicit package targets the intent


def test_connect_app_via_deeplink_builds_correct_uri(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        avd, "deeplink", lambda path, serial=None, package=None: seen.update(path=path)
    )
    avd.connect_app_via_deeplink("t192.168.1.168:4403")
    assert seen["path"] == "connections?address=t192.168.1.168:4403"


def test_disconnect_app_via_deeplink_uses_sentinel(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        avd, "deeplink", lambda path, serial=None, package=None: seen.update(path=path)
    )
    avd.disconnect_app_via_deeplink()
    assert seen["path"] == f"connections?address={avd.NO_DEVICE_SELECTED}"
    assert avd.NO_DEVICE_SELECTED == "n"


def test_connect_app_to_tcp_deeplink_fast_path_confirms(monkeypatch) -> None:
    # "Not connected" is present for the first 2 polls, then clears -> success
    # without ever falling through to the legacy UI-tap flow.
    calls = {"deeplink": 0, "tap": 0, "find_text": 0}

    def _find_text(token, serial=None):
        calls["find_text"] += 1
        return calls["find_text"] <= 2

    monkeypatch.setattr(
        avd,
        "connect_app_via_deeplink",
        lambda addr, serial=None: calls.__setitem__("deeplink", calls["deeplink"] + 1),
    )
    monkeypatch.setattr(avd, "find_text", _find_text)
    monkeypatch.setattr(
        avd, "_tap_text", lambda *a, **k: calls.__setitem__("tap", calls["tap"] + 1) or True
    )
    monkeypatch.setattr(avd.time, "sleep", lambda s: None)  # don't actually wait in a unit test

    ok = avd.connect_app_to_tcp(host="192.168.1.168", port=4403, confirm_timeout_s=5.0)
    assert ok is True
    assert calls["deeplink"] == 1
    assert calls["tap"] == 0  # never fell through to the UI-tap fallback


def test_connect_app_to_tcp_falls_back_to_ui_taps_when_deeplink_never_confirms(monkeypatch) -> None:
    # "Not connected" never clears within the confirm window -> falls through to
    # the legacy UI-tap flow (covers app builds predating the deep link).
    monkeypatch.setattr(avd, "connect_app_via_deeplink", lambda addr, serial=None: None)
    monkeypatch.setattr(avd, "find_text", lambda token, serial=None: True)  # always "Not connected"
    monkeypatch.setattr(avd, "_tap_text", lambda *a, **k: False)  # "Skip" not found -> loop exits
    monkeypatch.setattr(
        avd, "_find_center", lambda *a, **k: None
    )  # "Add device manually" not found
    monkeypatch.setattr(avd.time, "sleep", lambda s: None)

    ok = avd.connect_app_to_tcp(host="192.168.1.168", port=4403, confirm_timeout_s=0.01)
    assert ok is False  # UI-tap flow ran but "Add device manually" was never found
