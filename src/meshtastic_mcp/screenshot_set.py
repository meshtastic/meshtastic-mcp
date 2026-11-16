# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Matching screenshots of the Android and iOS apps on one simulated mesh.

Each platform gets its own replay session (a replay node serves one app at a time),
both generated from the same seed and stamped from one clock anchor, so the apps
hold the same nodes, channels, positions, last-heard times and messages. Each
session plays its capture once, fast, then holds the connection quiet while both
apps are captured. Both devices report the mesh's center as their own location.
Navigation is by deep link, which both apps accept for the screens in `SCREENS`.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLATFORMS = ("android", "ios")
APPEARANCES = ("light", "dark")

# Settings pages both apps route by the same name.
_SHARED_SETTINGS = (
    "about",
    "bluetooth",
    "device",
    "display",
    "lora",
    "mqtt",
    "network",
    "position",
    "power",
    "security",
    "serial",
    "telemetry",
    "user",
)

# logical screen -> (Android deep-link path, iOS deep-link path)
SCREENS: dict[str, tuple[str, str]] = {
    "nodes": ("nodes", "nodes"),
    "map": ("map", "map"),
    "messages": ("messages", "messages"),
    "connections": ("connections", "connect"),
    "settings": ("settings", "settings"),
    **{f"settings/{p}": (f"settings/{p}", f"settings/{p}") for p in _SHARED_SETTINGS},
}


def resolve_screen(spec: str | dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Map a screen spec to `(name, {platform: deep-link path})`.

    `spec` is a key of `SCREENS`, `node:<num>` (node details), `channel:<index>`
    (a channel's conversation), or `{"name", "android", "ios"}` with explicit paths.
    """
    if isinstance(spec, dict):
        name = str(spec.get("name") or "")
        paths = {p: str(spec[p]).lstrip("/") for p in PLATFORMS if spec.get(p)}
        if not name or not paths:
            raise ValueError("a custom screen needs `name` and at least one platform path")
        return name, paths
    if spec in SCREENS:
        android, ios = SCREENS[spec]
        return spec.replace("/", "-"), {"android": android, "ios": ios}
    kind, _, arg = spec.partition(":")
    if kind == "node" and arg.isdigit():
        return f"node-{arg}", {"android": f"nodes/{arg}", "ios": f"nodes?nodenum={arg}"}
    if kind == "channel" and arg.isdigit():
        return (
            f"channel-{arg}",
            {"android": f"messages/{arg}^all", "ios": f"messages?channelId={arg}"},
        )
    known = ", ".join(sorted(SCREENS))
    raise ValueError(
        f"unknown screen {spec!r}: use one of {known}, node:<num>, channel:<index>, "
        'or {"name", "android", "ios"} with deep-link paths'
    )


class AndroidDriver:
    """The Android emulator, through `avd`."""

    name = "android"

    def __init__(self, serial: str | None) -> None:
        from .emulator import avd

        self._avd = avd
        self.serial = serial or avd.first_emulator_serial()
        if not self.serial:
            raise RuntimeError("no Android emulator is running (see `list_devices`)")
        package = avd.resolve_package(serial=self.serial)
        if not package:
            raise RuntimeError("no Meshtastic app is installed on the emulator")
        self.package: str = package
        self.app = package

    def _shell(self, *args: str) -> None:
        self._avd.adb("shell", *args, serial=self.serial, check=False)

    def reset_app(self) -> None:
        """Clear the app's data, regrant permissions and launch past onboarding."""
        if not self.package.endswith(".debug"):
            raise RuntimeError(
                f"{self.package} is not a debug build: it ignores the onboarding skip, "
                "so a reset would leave it on the intro screens"
            )
        self._shell("pm", "clear", self.package)
        self._avd.grant_runtime_permissions(self.package, serial=self.serial)
        self._avd.launch_app(self.package, serial=self.serial, skip_onboarding=True)
        time.sleep(3.0)

    def set_location(self, lat: float, lon: float) -> None:
        # The emulator console takes longitude first.
        self._avd.adb("emu", "geo", "fix", f"{lon:.7f}", f"{lat:.7f}", serial=self.serial)

    def clear_location(self) -> None:
        """The emulator console cannot unset a fix; the mesh's position stays."""

    def connect(self, port: int) -> None:
        self._avd.connect_app_via_deeplink(
            f"t10.0.2.2:{port}", serial=self.serial, package=self.package
        )
        if not self._avd.tap_text("Connect", serial=self.serial, timeout=10.0):
            raise RuntimeError("no connect prompt appeared after the connect link")

    def appearance(self, mode: str) -> None:
        self._shell("cmd", "uimode", "night", "yes" if mode == "dark" else "no")

    def clean_status_bar(self) -> None:
        self._shell("settings", "put", "global", "sysui_demo_allowed", "1")
        for extra in (
            ("enter",),
            ("clock", "-e", "hhmm", "0941"),
            ("battery", "-e", "level", "100", "-e", "plugged", "false"),
            ("network", "-e", "wifi", "show", "-e", "level", "4"),
            ("network", "-e", "mobile", "show", "-e", "level", "4"),
            ("notifications", "-e", "visible", "false"),
        ):
            command, *rest = extra
            self._shell(
                "am",
                "broadcast",
                "-a",
                "com.android.systemui.demo",
                "-e",
                "command",
                command,
                *rest,
            )

    def reset_status_bar(self) -> None:
        self._shell("am", "broadcast", "-a", "com.android.systemui.demo", "-e", "command", "exit")

    def open(self, path: str) -> None:
        self._avd.deeplink(path, serial=self.serial, package=self.package, clear_task=True)

    def screenshot(self, out: Path) -> None:
        self._avd.screenshot(out, serial=self.serial)


class IosDriver:
    """The iOS Simulator, through `apple_sim`. UI confirmation needs idb."""

    name = "ios"
    bundle_id = "gvh.MeshtasticClient"

    def __init__(self, udid: str | None) -> None:
        from .emulator import apple_sim

        self._sim = apple_sim
        self.udid = apple_sim.ensure_booted(udid=udid) if udid else apple_sim.ensure_booted()
        if not apple_sim.has_idb():
            raise RuntimeError("iOS needs idb to confirm the connect prompt (run `doctor`)")
        if not apple_sim.is_app_installed(self.bundle_id, udid=self.udid):
            raise RuntimeError(f"{self.bundle_id} is not installed on simulator {self.udid}")
        self.app = self.bundle_id

    def reset_app(self) -> None:
        """Return the app's settings to their defaults; its stored data stays."""
        self._sim.reset_app_prefs(self.bundle_id, udid=self.udid)
        self._sim.launch(self.bundle_id, udid=self.udid)
        time.sleep(3.0)

    def set_location(self, lat: float, lon: float) -> None:
        self._sim.set_location(lat, lon, udid=self.udid)

    def clear_location(self) -> None:
        self._sim.clear_location(udid=self.udid)

    def connect(self, port: int) -> None:
        self._sim.open_url(self._sim.connect_link(port), udid=self.udid)
        self._sim.dismiss_open_sheet(udid=self.udid)
        if not self._sim.tap_label("Connect", udid=self.udid, timeout=10.0):
            raise RuntimeError(
                "no connect prompt: the app needs Meshtastic-Apple 2.7.28 or later and "
                "must be past onboarding"
            )

    def appearance(self, mode: str) -> None:
        self._sim.set_appearance(mode, udid=self.udid)

    def clean_status_bar(self) -> None:
        self._sim.clean_status_bar(udid=self.udid)

    def reset_status_bar(self) -> None:
        self._sim.reset_status_bar(udid=self.udid)

    def open(self, path: str) -> None:
        self._sim.open_url(f"meshtastic://meshtastic/{path}", udid=self.udid)
        self._sim.dismiss_open_sheet(udid=self.udid)

    def screenshot(self, out: Path) -> None:
        self._sim.screenshot(out, udid=self.udid)


@dataclass
class Plan:
    screens: list[tuple[str, dict[str, str]]]
    platforms: list[str]
    appearances: list[str]
    out_dir: Path
    android_serial: str | None = None
    ios_udid: str | None = None
    sim_nodes: int = 60
    sim_seed: int = 1337
    sim_days: int = 1
    settle_s: float = 3.0
    # How much of the capture's end is streamed (the node DB covers the rest), and how fast.
    stream_hours: float = 24.0
    stream_rate: float = 120.0
    connect_wait_s: float = 150.0
    clean_status_bar: bool = True
    reset_apps: bool = False
    shots: list[dict[str, Any]] = field(default_factory=list)


def make_plan(
    screens: list[str | dict[str, Any]],
    platforms: list[str],
    appearances: list[str],
    out_dir: Path,
    **kwargs: Any,
) -> Plan:
    if not screens:
        raise ValueError("pass at least one screen")
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad or not platforms:
        raise ValueError(f"platforms must be a non-empty subset of {PLATFORMS}, got {platforms}")
    bad = [a for a in appearances if a not in APPEARANCES]
    if bad or not appearances:
        raise ValueError(f"appearances must be a non-empty subset of {APPEARANCES}")
    resolved = [resolve_screen(s) for s in screens]
    if not any(p in paths for _, paths in resolved for p in platforms):
        raise ValueError(f"no screen has a path for platforms {platforms}")
    return Plan(resolved, platforms, appearances, out_dir, **kwargs)


def _log(log_path: Path, msg: str) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


_RUN_LOCK = threading.Lock()


def run(plan: Plan, state: dict[str, Any], log_path: Path) -> None:
    """Job body: serve the mesh, connect each app, capture every screen and appearance."""
    # Runs share the emulator and simulator, so a second run waits for the first.
    if not _RUN_LOCK.acquire(blocking=False):
        _log(log_path, "waiting for another screenshot_set run")
        _RUN_LOCK.acquire()
    try:
        _run(plan, state, log_path)
    finally:
        _RUN_LOCK.release()


def _run(plan: Plan, state: dict[str, Any], log_path: Path) -> None:
    from . import jobs
    from .replay import ReplayParams, get_manager, sim

    plan.out_dir.mkdir(parents=True, exist_ok=True)
    manager = get_manager()
    anchor = int(time.time())
    start = anchor - plan.sim_days * 86400
    # Both apps keep a node database per connected node, so a fresh node number each
    # run (one for both apps) keeps nodes from earlier runs out of the captures.
    observer_num = random.randint(0x10000000, 0x7FFFFFFF)
    drivers: dict[str, Any] = {}
    sessions: list[str] = []
    try:
        for platform in plan.platforms:
            try:
                drivers[platform] = (
                    AndroidDriver(plan.android_serial)
                    if platform == "android"
                    else IosDriver(plan.ios_udid)
                )
            except Exception as exc:
                _log(log_path, f"{platform}: skipped, {exc}")
        if not drivers:
            raise RuntimeError("no platform could be driven")

        cap = sim.generate(
            nodes=plan.sim_nodes, days=plan.sim_days, seed=plan.sim_seed, start=start
        )
        center = cap.center()
        for platform, driver in drivers.items():
            _log(log_path, f"{platform}: app {driver.app}")
            if plan.reset_apps:
                driver.reset_app()
                _log(log_path, f"{platform}: app settings reset")
            if center is not None:
                driver.set_location(center[0] / 1e7, center[1] / 1e7)
            status = manager.start(
                cap,
                ReplayParams(
                    host="0.0.0.0",
                    port=0,
                    start=anchor - int(plan.stream_hours * 3600),
                    rate=plan.stream_rate,
                    loop=False,
                    hold=True,
                    mdns=False,
                    observer_num=observer_num,
                    clock_anchor=anchor,
                ),
            )
            sessions.append(status["id"])
            port = int(status["listen"].rsplit(":", 1)[1])
            _log(log_path, f"{platform}: replay {status['id']} on port {port}")
            driver.connect(port)

        # Wait until each app's current connection has the whole window. An app that
        # hangs up (Meshtastic-Apple sometimes does, right after connecting) reconnects
        # and is played the window again; the sessions then hold quiet.
        deadline = time.time() + plan.connect_wait_s
        while time.time() < deadline:
            states = [manager.status(sid) for sid in sessions]
            if all(s.get("connected") and s.get("pass_complete") for s in states):
                break
            time.sleep(1.0)
        for sid, platform in zip(sessions, drivers, strict=True):
            s = manager.status(sid)
            _log(
                log_path,
                f"{platform}: connected={s.get('connected')} "
                f"window received={s.get('pass_complete')} sent {s.get('packets_sent')}",
            )
            if not (s.get("connected") and s.get("pass_complete")):
                raise RuntimeError(
                    f"{platform} never held a replay connection through the whole window: "
                    f"{s.get('error')}"
                )
        time.sleep(plan.settle_s * 2)  # let both apps finish writing what they received

        if plan.clean_status_bar:
            for driver in drivers.values():
                driver.clean_status_bar()
        for mode in plan.appearances:
            for driver in drivers.values():
                driver.appearance(mode)
            for name, paths in plan.screens:
                for platform, driver in drivers.items():
                    path = paths.get(platform)
                    if path is None:
                        continue
                    driver.open(path)
                time.sleep(plan.settle_s)
                for platform, driver in drivers.items():
                    if platform not in paths:
                        continue
                    out = plan.out_dir / name / f"{platform}-{mode}.png"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    driver.screenshot(out)
                    shot = {"screen": name, "platform": platform, "appearance": mode}
                    shot["path"] = str(out)
                    with jobs.LOCK:
                        state["artifacts"].append(str(out))
                    plan.shots.append(shot)
                    _log(log_path, f"{name} {platform} {mode}: {out}")
        with jobs.LOCK:
            state["status"] = "done"
            state["exit_code"] = 0
            state["finished_at"] = time.time()
    finally:
        for sid in sessions:
            manager.stop(sid)
        for driver in drivers.values():
            try:
                driver.clear_location()
                if plan.clean_status_bar:
                    driver.reset_status_bar()
            except Exception as exc:  # cleanup must not mask the job's own error
                _log(log_path, f"{driver.name}: cleanup failed: {exc}")
