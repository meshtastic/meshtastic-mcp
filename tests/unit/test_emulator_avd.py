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


def test_type_text_adb_fallback_encodes_spaces(monkeypatch) -> None:
    # Without the [android-fast] extra, type_text uses `adb input text`, which
    # needs spaces as %s. The encoding must happen HERE (not before the u2 fast
    # path, which pastes verbatim and would otherwise send a literal "%s").
    monkeypatch.setattr(avd.u2, "available", lambda: False)
    calls = []
    monkeypatch.setattr(avd, "adb", lambda *a, **k: calls.append(a) or _cp(""))
    avd.type_text("hello world", serial="emulator-5554")
    assert calls == [("shell", "input", "text", "hello%sworld")]


def test_type_text_raises_on_fast_path_failure_no_adb_replay(monkeypatch) -> None:
    # A uiautomator2 failure mid-type may have already inserted the text; the adb
    # path must NOT run (double-insert). Reset + raise instead of falling back.
    monkeypatch.setattr(avd.u2, "available", lambda: True)

    def _boom(text, serial=None):
        raise RuntimeError("u2 lost response")

    monkeypatch.setattr(avd.u2, "send_keys", _boom)
    reset_calls, adb_calls = [], []
    monkeypatch.setattr(avd.u2, "reset", lambda serial=None: reset_calls.append(serial))
    monkeypatch.setattr(avd, "adb", lambda *a, **k: adb_calls.append(a) or _cp(""))
    with pytest.raises(avd.EmulatorError, match="not retried on adb"):
        avd.type_text("secret", serial="emulator-5554")
    assert reset_calls == ["emulator-5554"]
    assert adb_calls == []  # never replayed


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
    avd.connect_app_via_deeplink("t192.0.2.68:4403")
    assert seen["path"] == "connections?address=t192.0.2.68:4403"


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

    ok = avd.connect_app_to_tcp(host="192.0.2.68", port=4403, confirm_timeout_s=5.0)
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

    ok = avd.connect_app_to_tcp(host="192.0.2.68", port=4403, confirm_timeout_s=0.01)
    assert ok is False  # UI-tap flow ran but "Add device manually" was never found


def test_parse_uiautomator_xml_includes_bounds_and_label() -> None:
    xml = (
        '<?xml version="1.0"?><hierarchy>'
        '<node text="A" bounds="[10,20][110,220]">'
        '<node text="B" bounds="[10,20][60,70]" clickable="true"/>'
        "</node>"
        "</hierarchy>"
    )
    els = avd._parse_uiautomator_xml(xml)
    assert els[0]["text"] == "A"
    assert els[0]["bounds"] == [10, 20, 110, 220]
    assert els[0]["label"] == 1
    assert els[1]["text"] == "B"
    assert els[1]["bounds"] == [10, 20, 60, 70]
    assert els[1]["label"] == 2


def test_parse_uiautomator_xml_skips_label_without_bounds() -> None:
    xml = '<?xml version="1.0"?><hierarchy><node text="no-bounds"/></hierarchy>'
    els = avd._parse_uiautomator_xml(xml)
    assert "bounds" not in els[0]
    assert "label" not in els[0]


def test_annotate_screenshot_draws_boxes(tmp_path) -> None:
    pytest.importorskip("PIL")  # Pillow is the [ui] extra; skip when absent
    from PIL import Image

    png_path = tmp_path / "shot.png"
    Image.new("RGB", (200, 200), color="white").save(png_path)
    elements = [
        {"text": "Send", "bounds": [10, 10, 60, 40], "label": 1},
        {"text": "no-bounds-no-label"},
    ]
    avd.annotate_screenshot(png_path, elements)
    img = Image.open(png_path)
    # The box outline was drawn in red along the top edge of element 1's bounds.
    assert img.getpixel((10, 10))[0] > 200  # red channel high at the box corner
    assert img.getpixel((10, 10))[1] < 100  # green channel low (not white anymore)


@pytest.fixture(autouse=True)
def _clear_last_annotated():
    # _LAST_ANNOTATED is process-global cache state (keyed by serial); tests
    # in this module set arbitrary serials, but clear before each test so a
    # leftover key from one test can never leak into another's assertions.
    avd._LAST_ANNOTATED.clear()
    yield
    avd._LAST_ANNOTATED.clear()


def test_resolve_label_physical_uses_cached_elements(monkeypatch) -> None:
    avd._LAST_ANNOTATED["phys-serial"] = {
        "screenshot": "/tmp/whatever.png",
        "elements": [{"text": "Send", "bounds": [10, 10, 60, 40], "label": 1}],
    }
    assert avd.resolve_label(1, serial="phys-serial") == (35, 25)


def test_resolve_label_physical_missing_label_raises() -> None:
    avd._LAST_ANNOTATED["phys-serial"] = {
        "screenshot": "/tmp/whatever.png",
        "elements": [{"text": "Send", "bounds": [10, 10, 60, 40], "label": 1}],
    }
    with pytest.raises(avd.EmulatorError, match="not found"):
        avd.resolve_label(99, serial="phys-serial")


def test_resolve_label_no_screenshot_yet_raises() -> None:
    with pytest.raises(avd.EmulatorError, match="no annotated screenshot"):
        avd.resolve_label(1, serial="never-captured")


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color="white").save(buf, format="PNG")
    return buf.getvalue()


def test_screenshot_plain_capture_invalidates_stale_annotation_cache(monkeypatch, tmp_path) -> None:
    pytest.importorskip("PIL")  # Pillow is the [ui] extra; skip when absent
    # Regression: a plain (annotate=False) capture at the same path/serial must
    # invalidate any cached annotation, or resolve_label keeps resolving against
    # stale/wrong content after the on-disk file is no longer annotated.
    serial = "R5CT80ABCDE"  # physical (non-emulator) serial
    png_bytes = _png_bytes()

    def fake_run(cmd, *, stdout, stderr, timeout):
        stdout.write(png_bytes)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(avd, "_adb_bin", lambda: "adb")
    monkeypatch.setattr(avd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        avd,
        "ui_dump",
        lambda serial=None: [
            {
                "text": "Send",
                "bounds": [10, 10, 60, 40],
                "label": 1,
                "interactions": ["clickable"],
            }
        ],
    )

    out_path = tmp_path / "shot.png"

    avd.screenshot(out_path, serial=serial, annotate=True)
    assert avd.resolve_label(1, serial=serial) == (35, 25)

    # A subsequent plain capture at the same serial must clear the cache.
    avd.screenshot(out_path, serial=serial, annotate=False)
    with pytest.raises(avd.EmulatorError, match="no annotated screenshot"):
        avd.resolve_label(1, serial=serial)


def test_resolve_label_emulator_shells_out_to_android_resolve(monkeypatch) -> None:
    avd._LAST_ANNOTATED["emulator-5554"] = {
        "screenshot": "/tmp/ui.png",
        "elements": None,
    }
    calls = []

    def fake_android(*args, **kwargs):
        calls.append(args)
        return _cp("input tap 500 1000")

    monkeypatch.setattr(avd, "android", fake_android)
    coords = avd.resolve_label(5, serial="emulator-5554")
    assert coords == (500, 1000)
    assert calls[0] == (
        "screen",
        "resolve",
        "--screenshot",
        "/tmp/ui.png",
        "--string",
        "#5",
    )
