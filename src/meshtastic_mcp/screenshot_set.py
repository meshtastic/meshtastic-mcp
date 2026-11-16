# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Matching screenshots of the Android and iOS apps on one simulated mesh.

Each platform gets its own replay session (a replay node serves one app at a time),
both generated from the same seed and start, so the apps hold the same nodes,
channels and positions. Streaming is slowed to a trickle so the screens stay put
while both apps are captured. Navigation is by deep link, which both apps accept
for the screens in `SCREENS`.

Times shown relative to now ("2 min ago") and the order of nodes heard at the same
moment can still differ between the two apps.
"""

from __future__ import annotations

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

    def _shell(self, *args: str) -> None:
        self._avd.adb("shell", *args, serial=self.serial, check=False)

    def connect(self, port: int) -> None:
        self._avd.connect_app_via_deeplink(f"t10.0.2.2:{port}", serial=self.serial)

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
        self._avd.deeplink(path, serial=self.serial)

    def screenshot(self, out: Path) -> None:
        self._avd.screenshot(out, serial=self.serial)


class IosDriver:
    """The iOS Simulator, through `apple_sim`. UI confirmation needs idb."""

    name = "ios"

    def __init__(self, udid: str | None) -> None:
        from .emulator import apple_sim

        self._sim = apple_sim
        self.udid = apple_sim.ensure_booted(udid=udid) if udid else apple_sim.ensure_booted()
        if not apple_sim.has_idb():
            raise RuntimeError("iOS needs idb to confirm the connect prompt (run `doctor`)")

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
    connect_wait_s: float = 45.0
    clean_status_bar: bool = True
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
    return Plan([resolve_screen(s) for s in screens], platforms, appearances, out_dir, **kwargs)


def _log(log_path: Path, msg: str) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def run(plan: Plan, state: dict[str, Any], log_path: Path) -> None:
    """Job body: serve the mesh, connect each app, capture every screen and appearance."""
    from . import jobs
    from .replay import ReplayParams, get_manager, sim

    plan.out_dir.mkdir(parents=True, exist_ok=True)
    manager = get_manager()
    start = int(time.time()) - plan.sim_days * 86400
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

        for platform, driver in drivers.items():
            cap = sim.generate(
                nodes=plan.sim_nodes, days=plan.sim_days, seed=plan.sim_seed, start=start
            )
            status = manager.start(
                cap,
                ReplayParams(host="0.0.0.0", port=0, rate=0.2, loop=True, mdns=False),
            )
            sessions.append(status["id"])
            port = int(status["listen"].rsplit(":", 1)[1])
            _log(log_path, f"{platform}: replay {status['id']} on port {port}")
            driver.connect(port)

        deadline = time.time() + plan.connect_wait_s
        while time.time() < deadline:
            states = [manager.status(sid) for sid in sessions]
            if all(s.get("connected") and s.get("packets_sent", 0) > 0 for s in states):
                break
            time.sleep(1.0)
        for sid, platform in zip(sessions, drivers, strict=True):
            s = manager.status(sid)
            _log(log_path, f"{platform}: connected={s.get('connected')}")
        time.sleep(plan.settle_s * 2)  # let both apps finish drawing the node list

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
        if plan.clean_status_bar:
            for driver in drivers.values():
                try:
                    driver.reset_status_bar()
                except Exception as exc:  # cleanup must not mask the job's own error
                    _log(log_path, f"status bar reset failed: {exc}")
