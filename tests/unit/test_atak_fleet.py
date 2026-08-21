# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""atak.py pure helpers: AVD cloning, port allocation, pref XML, route math."""

from __future__ import annotations

import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from meshtastic_mcp.emulator import atak


# ---------------------------------------------------------------------------
# clone_avd
# ---------------------------------------------------------------------------
def _fake_avd(home: Path, name: str) -> None:
    d = home / f"{name}.avd"
    d.mkdir(parents=True)
    (d / "config.ini").write_text(f"AvdId={name}\navd.ini.displayname={name}\nhw.ramSize=2048\n")
    (d / "userdata.img").write_bytes(b"disk")
    (home / f"{name}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={d}\npath.rel=avd/{name}.avd\n"
    )


def test_clone_avd_copies_and_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atak, "_avd_home", lambda: tmp_path)
    _fake_avd(tmp_path, "base")

    atak.clone_avd("base", "atak-node-0")

    ini = (tmp_path / "atak-node-0.ini").read_text()
    assert f"path={tmp_path / 'atak-node-0.avd'}" in ini
    assert "path.rel=avd/atak-node-0.avd" in ini
    cfg = (tmp_path / "atak-node-0.avd" / "config.ini").read_text()
    assert "AvdId=atak-node-0" in cfg
    assert "avd.ini.displayname=atak-node-0" in cfg
    assert "hw.ramSize=2048" in cfg  # untouched settings survive
    assert (tmp_path / "atak-node-0.avd" / "userdata.img").read_bytes() == b"disk"


def test_clone_avd_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atak, "_avd_home", lambda: tmp_path)
    _fake_avd(tmp_path, "base")
    atak.clone_avd("base", "n")
    marker = tmp_path / "n.avd" / "provisioned-marker"
    marker.write_text("keep me")
    atak.clone_avd("base", "n")  # second call must not wipe the clone
    assert marker.read_text() == "keep me"


def test_clone_avd_missing_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atak, "_avd_home", lambda: tmp_path)
    with pytest.raises(atak.AtakError, match="not found"):
        atak.clone_avd("nope", "x")


def test_clone_avd_strips_boot_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A base AVD that has been booted carries lock files + hardware-qemu.ini +
    # snapshots; the clone must not inherit them (else it refuses to boot or
    # corrupts the base's userdata).
    monkeypatch.setattr(atak, "_avd_home", lambda: tmp_path)
    base = tmp_path / "base.avd"
    base.mkdir(parents=True)
    (base / "config.ini").write_text("AvdId=base\n")
    (tmp_path / "base.ini").write_text(f"path={base}\npath.rel=avd/base.avd\n")
    (base / "hardware-qemu.ini").write_text("stale")
    (base / "multiinstance.lock").write_text("pid")
    lockdir = base / "userdata-qemu.img.lock"  # a lock can be a dir
    lockdir.mkdir()
    (lockdir / "pid").write_text("123")
    (base / "snapshots").mkdir()
    (base / "snapshots" / "default_boot").mkdir()

    atak.clone_avd("base", "n")

    dest = tmp_path / "n.avd"
    assert not (dest / "hardware-qemu.ini").exists()
    assert not (dest / "multiinstance.lock").exists()
    assert not (dest / "userdata-qemu.img.lock").exists()
    assert not (dest / "snapshots").exists()
    assert (dest / "config.ini").exists()  # real content survives


# ---------------------------------------------------------------------------
# alloc_console_port
# ---------------------------------------------------------------------------
def test_alloc_console_port_skips_busy() -> None:
    # Occupy 5554 the way a real emulator does — a LISTENING socket. (A merely
    # bound, non-listening socket with SO_REUSEADDR doesn't block _port_free's
    # own SO_REUSEADDR bind, so it must listen to be genuinely "busy".)
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", 5554))
            s.listen(1)
        except OSError:
            pytest.skip("5554 already in use by a real emulator")
        assert atak.alloc_console_port(0) == 5556


def test_alloc_console_port_index_spacing() -> None:
    p0 = atak.alloc_console_port(0)
    p1 = atak.alloc_console_port(1)
    assert p0 % 2 == 0 and p1 % 2 == 0
    assert p1 >= 5556


# ---------------------------------------------------------------------------
# stream_pref
# ---------------------------------------------------------------------------
def test_stream_pref_is_valid_atak_config() -> None:
    xml = atak.stream_pref("10.0.2.2", 8087)
    root = ET.fromstring(xml)
    pref = root.find("preference")
    assert pref is not None and pref.get("name") == "cot_streams"
    entries = {e.get("key"): e.text for e in pref.findall("entry")}
    assert entries["connectString0"] == "10.0.2.2:8087:tcp"
    assert entries["count"] == "1"
    assert entries["enabled0"] == "true"
    assert entries["useAuth0"] == "false"


# ---------------------------------------------------------------------------
# route interpolation
# ---------------------------------------------------------------------------
def test_leg_meters_known_distance() -> None:
    # 0.01 deg latitude ~= 1113 m regardless of longitude.
    d = atak._leg_meters((41.60, -93.77), (41.61, -93.77))
    assert 1100 < d < 1130


def test_interp_endpoints_and_count() -> None:
    pts = atak._interp((0.0, 0.0), (1.0, 2.0), 4)
    assert len(pts) == 4
    assert pts[0] == (0.0, 0.0)
    lat, lon = pts[-1]
    assert lat == pytest.approx(0.75)
    assert lon == pytest.approx(1.5)


def test_drive_route_requires_two_waypoints() -> None:
    with pytest.raises(atak.AtakError, match="at least 2"):
        atak.drive_route("emulator-5554", [(1.0, 2.0)])


@pytest.mark.parametrize("bad", [0.0, -5.0, float("inf"), float("nan")])
def test_drive_route_rejects_bad_speed(bad: float) -> None:
    # A non-positive/non-finite speed would clamp the interpolation denominator
    # to 1e-6 and spawn ~a billion points (OOM); reject before interpolation.
    with pytest.raises(atak.AtakError, match="speed_mps"):
        atak.drive_route("emulator-5554", [(0.0, 0.0), (0.01, 0.0)], speed_mps=bad)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_drive_route_rejects_bad_step(bad: float) -> None:
    with pytest.raises(atak.AtakError, match="step_s"):
        atak.drive_route("emulator-5554", [(0.0, 0.0), (0.01, 0.0)], step_s=bad)


def test_logcat_argv_filters_to_atak_tag() -> None:
    argv = atak.logcat_argv("emulator-5554")
    assert argv[:4] == ["adb", "-s", "emulator-5554", "logcat"]
    # Only the ATAK comms tag shown, everything else silenced (kills GL noise).
    assert f"{atak.ATAK_LOG_TAG}:V" in argv
    assert "*:S" in argv
    assert "-d" not in argv  # follow by default
    assert "-d" in atak.logcat_argv("emulator-5554", follow=False)


def test_provision_tag_keyed_on_apk_bytes(tmp_path: Path) -> None:
    apk1 = tmp_path / "a.apk"
    apk2 = tmp_path / "b.apk"
    apk1.write_bytes(b"build-1")
    apk2.write_bytes(b"build-2")
    t1, t2 = atak.provision_tag(str(apk1)), atak.provision_tag(str(apk2))
    assert t1 != t2
    assert t1.startswith("provisioned_")
    assert t1 == atak.provision_tag(str(apk1))  # stable


# ---------------------------------------------------------------------------
# wait_for_boot
# ---------------------------------------------------------------------------
class _Adb:
    def __init__(self, props: dict[str, str]) -> None:
        self.props = props

    def __call__(self, *args: str, **kw: object) -> object:
        class R:
            stdout = ""

        r = R()
        if args[:2] == ("shell", "getprop"):
            r.stdout = self.props.get(args[2], "")
        return r


def test_wait_for_boot_accepts_no_boot_anim(monkeypatch: pytest.MonkeyPatch) -> None:
    # `-no-boot-anim` (a fleet flag) means the bootanim service never starts,
    # so the prop is empty rather than "stopped" — must still count as booted.
    monkeypatch.setattr(atak.avd, "adb", _Adb({"sys.boot_completed": "1"}))
    atak.wait_for_boot("emulator-5554", timeout=1)


def test_wait_for_boot_waits_while_anim_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        atak.avd, "adb", _Adb({"sys.boot_completed": "1", "init.svc.bootanim": "running"})
    )
    monkeypatch.setattr(atak.time, "sleep", lambda _s: None)
    with pytest.raises(atak.AtakError):
        atak.wait_for_boot("emulator-5554", timeout=0.2)


# ---------------------------------------------------------------------------
# first-run walk resilience
# ---------------------------------------------------------------------------
def test_tap_label_tolerates_bad_layout_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    # `android layout` intermittently returns non-JSON on an animating screen;
    # that must read as "label not on screen", not abort provisioning.
    def boom(*_a: object, **_k: object) -> None:
        raise atak.avd.EmulatorError("layout returned non-JSON")

    monkeypatch.setattr(atak.avd, "ui_dump", boom)
    assert atak._tap_label("emulator-5554", "I agree.") is False


def test_walk_first_run_dismisses_anr(monkeypatch: pytest.MonkeyPatch) -> None:
    # A starved emulator throws "System UI isn't responding" over ATAK's EULA;
    # the walk must tap Wait (not Close app) and carry on.
    screen = {"texts": ["System UI isn't responding", "Close app", "Wait"]}
    taps: list[str] = []

    def ui_dump(*, serial: str | None = None, diff: bool = False) -> list[dict[str, object]]:
        return [
            {"text": t, "interactions": ["clickable"], "center": [1, i]}
            for i, t in enumerate(screen["texts"])
        ]

    def tap(x: int, y: int, *, serial: str | None = None) -> None:
        label = screen["texts"][y]
        taps.append(label)
        if label == "Wait":
            screen["texts"] = ["I agree.", "Tools"]
        elif label == "I agree.":
            screen["texts"] = ["Tools"]

    monkeypatch.setattr(atak.avd, "ui_dump", ui_dump)
    monkeypatch.setattr(atak.avd, "tap", tap)
    monkeypatch.setattr(
        atak.avd, "find_text", lambda tok, *, serial=None: any(tok in t for t in screen["texts"])
    )
    monkeypatch.setattr(atak.time, "sleep", lambda _s: None)
    atak._walk_first_run("emulator-5554", timeout=5)
    assert taps == ["Wait", "I agree."]


def test_fleet_flags_give_play_image_headroom() -> None:
    # The Play system image runs GMS dex2oat + Play Store after boot; at 2 cores /
    # 2 GB the emulator ANRs through ATAK's first run.
    flags = list(atak._FLEET_FLAGS)
    assert flags[flags.index("-cores") + 1] == "3"
    assert flags[flags.index("-memory") + 1] == "4096"


def test_push_stream_pref_writes_the_defaults_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # ATAK only auto-loads config/prefs/defaults (no extension) at activity start
    # (PreferenceControl.ingestDefaults); a .pref there is inert.
    pushed: list[str] = []

    def adb(*args: str, **kw: object) -> None:
        if args[0] == "push":
            pushed.append(args[2])

    monkeypatch.setattr(atak.avd, "adb", adb)
    atak.push_stream_pref("emulator-5554", "10.0.2.2", 8087)
    assert pushed == ["/sdcard/atak/config/prefs/defaults"]


def test_drive_route_passes_ground_speed_to_geo_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    # ATAK reads Location.getSpeed(); the emulator only sets it from geo fix's
    # optional velocity (knots). Without it every PLI reports speed="0.0".
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(atak.avd, "adb", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(atak.time, "sleep", lambda _s: None)
    atak.drive_route("emulator-5554", [(41.71, -93.69), (41.7101, -93.69)], speed_mps=1.5, step_s=2)
    fix = next(c for c in calls if c[:3] == ("emu", "geo", "fix"))
    assert fix[5:] == ("280", "8", "2.9")  # alt m, satellites, knots (1.5 m/s)


def test_walk_first_run_survives_bad_dump_on_text_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # The ANR / "Tools" checks run before _tap_label's own guard; a bad dump there
    # must not abort provisioning.
    calls = {"n": 0}

    def find_text(tok: str, *, serial: str | None = None) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise atak.avd.EmulatorError("layout returned non-JSON")
        return tok == "Tools"

    monkeypatch.setattr(atak.avd, "find_text", find_text)
    monkeypatch.setattr(atak.avd, "ui_dump", lambda **_k: [])
    monkeypatch.setattr(atak.time, "sleep", lambda _s: None)
    atak._walk_first_run("emulator-5554", timeout=5)  # returns once "Tools" is seen


# defaults / quiesce / fleet_down + provision ordering
# ---------------------------------------------------------------------------
class _AdbLog:
    """Records every adb argv; captures the content of any pushed local file
    (the temp pref is unlinked right after the push, so read it now)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.pushed: list[tuple[str, str]] = []  # (dest, content)

    def __call__(self, *args: str, **kw: object) -> object:
        self.calls.append(args)
        if args[:1] == ("push",):
            self.pushed.append((args[2], Path(args[1]).read_text()))

        class R:
            stdout = ""

        return R()


def _entries(xml: str, name: str) -> dict[str | None, tuple[str | None, str | None]]:
    root = ET.fromstring(xml)
    for pref in root.findall("preference"):
        if pref.get("name") == name:
            return {e.get("key"): (e.get("class"), e.text) for e in pref.findall("entry")}
    raise AssertionError(f"no <preference name={name!r}> in {xml}")


def test_prefs_xml_types_and_groups() -> None:
    xml = atak.prefs_xml({"g": {"b": True, "f": False, "i": 5, "s": "Constant"}})
    got = _entries(xml, "g")
    assert got["b"] == ("class java.lang.Boolean", "true")
    assert got["f"] == ("class java.lang.Boolean", "false")
    assert got["i"] == ("class java.lang.Integer", "5")
    assert got["s"] == ("class java.lang.String", "Constant")
    with pytest.raises(atak.AtakError, match="unsupported"):
        atak.prefs_xml({"g": {"x": 1.5}})


def test_push_defaults_writes_single_defaults_file(monkeypatch: pytest.MonkeyPatch) -> None:
    adb = _AdbLog()
    monkeypatch.setattr(atak.avd, "adb", adb)
    atak.push_defaults("emulator-5554", {"cot_streams": atak.stream_entries("10.0.2.2", 8087)})
    assert [d for d, _ in adb.pushed] == ["/sdcard/atak/config/prefs/defaults"]
    assert _entries(adb.pushed[0][1], "cot_streams")["connectString0"][1] == "10.0.2.2:8087:tcp"
    assert adb.calls[0][:2] == ("push", adb.calls[0][1])  # local temp path
    assert not Path(adb.calls[0][1]).exists()  # temp pref cleaned up


def test_push_stream_pref_matches_stream_pref(monkeypatch: pytest.MonkeyPatch) -> None:
    adb = _AdbLog()
    monkeypatch.setattr(atak.avd, "adb", adb)
    atak.push_stream_pref("emulator-5554", "10.0.2.2", 8087)
    assert adb.pushed[0][1] == atak.stream_pref("10.0.2.2", 8087)


def test_quiesce_stops_clears_statesaver_and_disables_beacon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = _AdbLog()
    monkeypatch.setattr(atak.avd, "adb", adb)
    atak.quiesce("emulator-5556")

    assert adb.calls[0] == ("shell", "am", "force-stop", atak.ATAK_PACKAGE)
    assert adb.calls[1][:3] == ("shell", "rm", "-f")
    assert "/sdcard/atak/Databases/statesaver2.sqlite" in adb.calls[1]
    assert adb.calls[2][0] == "push"
    dest, xml = adb.pushed[0]
    assert dest == "/sdcard/atak/config/prefs/defaults"
    got = _entries(xml, "com.atakmap.app.civ_preferences")
    assert got["plugins.emergency.beacon.enabled"] == ("class java.lang.Boolean", "false")


def test_fleet_down_quiesces_each_node_before_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[tuple[str, str]] = []
    monkeypatch.setattr(atak, "quiesce", lambda serial: order.append(("quiesce", serial)))
    monkeypatch.setattr(
        atak.avd, "adb", lambda *a, serial=None, **kw: order.append((" ".join(a), serial))
    )
    fleet = atak.Fleet(
        nodes=[
            atak.FleetNode(name="atak-node-0", serial="emulator-5554", port=5554),
            atak.FleetNode(name="atak-node-1", serial="emulator-5556", port=5556),
        ]
    )
    atak.fleet_down(fleet)
    assert order == [
        ("quiesce", "emulator-5554"),
        ("emu kill", "emulator-5554"),
        ("quiesce", "emulator-5556"),
        ("emu kill", "emulator-5556"),
    ]


def test_fleet_down_kills_even_if_quiesce_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[str | None] = []

    def boom(serial: str) -> None:
        raise RuntimeError("adb wedged")

    monkeypatch.setattr(atak, "quiesce", boom)
    monkeypatch.setattr(atak.avd, "adb", lambda *a, serial=None, **kw: killed.append(serial))
    atak.fleet_down(atak.Fleet(nodes=[atak.FleetNode(name="n", serial="emulator-5554", port=5554)]))
    assert killed == ["emulator-5554"]


def test_provision_stages_defaults_once_then_quiesces_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = _AdbLog()
    order: list[str] = []
    monkeypatch.setattr(atak.avd, "adb", adb)
    monkeypatch.setattr(atak.avd, "is_app_installed", lambda pkg, serial=None: True)
    monkeypatch.setattr(atak.time, "sleep", lambda _s: None)
    monkeypatch.setattr(atak, "_walk_first_run", lambda serial, **kw: order.append("first_run"))
    monkeypatch.setattr(atak, "quiesce", lambda serial: order.append("quiesce"))
    monkeypatch.setattr(atak, "launch", lambda serial, **kw: order.append("launch"))

    atak.provision("emulator-5554", "/nonexistent.apk", relay_port=8087)

    assert order == ["first_run", "quiesce", "launch"]
    # One defaults file carrying the stream AND the hint/beacon suppression.
    assert [d for d, _ in adb.pushed] == ["/sdcard/atak/config/prefs/defaults"]
    xml = adb.pushed[0][1]
    assert _entries(xml, "cot_streams")["connectString0"][1] == "10.0.2.2:8087:tcp"
    quiet = _entries(xml, "com.atakmap.app.civ_preferences")
    for key in (
        "atak.hint.batoptimization.issue",
        "atak.hint.textContainer.osd",
        "atak.hint.iconset",
        "plugins.emergency.beacon.enabled",
    ):
        assert quiet[key] == ("class java.lang.Boolean", "false")
    # Doze whitelist precedes ATAK start, so the first launch never asks.
    whitelist = adb.calls.index(
        ("shell", "dumpsys", "deviceidle", "whitelist", "+com.atakmap.app.civ")
    )
    start = next(i for i, c in enumerate(adb.calls) if c[:3] == ("shell", "am", "start"))
    assert whitelist < start
