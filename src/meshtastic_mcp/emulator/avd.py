# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Android app-plane control for hardware-free (AVD) and physical-device e2e.

Wraps the `android` CLI (AVD lifecycle, emulator UI dump/screenshot) and `adb`
(input, install, TCP-connect nav).  Physical USB-attached devices are supported
alongside emulators:

  Emulator topology
    App inside AVD → ``10.0.2.2:<port>`` → host meshtasticd native node.

  Physical-device topology
    ``adb reverse tcp:<port> tcp:<port>`` tunnels host meshtasticd back to the
    phone; app connects to ``127.0.0.1:<port>``.

UI observation falls back from the emulator-specific ``android layout`` command
to ``adb exec-out uiautomator dump`` for physical devices; the returned dicts
share the same schema (text, interactions, center) so all helpers are
device-agnostic.

Requires ``adb`` on PATH (always).  The ``android`` CLI is only required for
AVD lifecycle operations; physical-device paths use only ``adb``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Host-loopback alias as seen from inside the Android emulator.
EMULATOR_HOST_ALIAS = "10.0.2.2"
DEFAULT_TCP_PORT = 4403

# Known Meshtastic-Android applicationIds, most-specific first. The fdroid debug
# build is preferred for e2e (no Play Services deps; TCP connect works everywhere).
# See scripts/build_android_apk.sh's --variant default (assembleFdroidDebug).
KNOWN_PACKAGES = (
    "com.geeksville.mesh.fdroid.debug",
    "com.geeksville.mesh.google.debug",
    "com.geeksville.mesh",
)

# meshtastic://meshtastic/{path} and https://meshtastic.org/{path} both resolve
# through the same in-app DeepLinkRouter (core/navigation). The custom scheme
# needs no App Link verification and is the fastest way to drive navigation from
# adb/automation. See docs/en/developer/navigation-and-deep-links.md.
DEEPLINK_SCHEME = "meshtastic://meshtastic"

# The app's internal "no device selected" sentinel — passing this as the
# `?address=` value on the /connections deep link disconnects instead of
# connecting (added alongside the connect-by-address deep link).
NO_DEVICE_SELECTED = "n"

# Fully-qualified launcher activity — needed for `am start -n <pkg>/<activity>`
# with intent extras (the onboarding-skip affordance below). Stable across the
# applicationId variants in KNOWN_PACKAGES (they share one app module).
MAIN_ACTIVITY = "org.meshtastic.app.MainActivity"

# Debug-build intent extra that skips the first-run intro flow on a fresh install
# (Meshtastic-Android#6044): `am start -n <pkg>/<activity> --ez skip_onboarding true`
# calls onAppIntroCompleted() straight away. BuildConfig.DEBUG-gated in the app —
# a no-op on release/Play builds. Pair with grant_runtime_permissions() to also
# skip the permission dialogs. Requires an app built at or after that PR.
EXTRA_SKIP_ONBOARDING = "skip_onboarding"

# Runtime permissions the app requests during onboarding. Pre-granting them with
# `pm grant` (native Android, no app code) pre-accepts the permission dialogs so a
# fresh-install automation run lands straight on the main screen. POST_NOTIFICATIONS
# is API 33+ (a no-op / error on older devices — grant is best-effort).
ONBOARDING_PERMISSIONS = (
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.POST_NOTIFICATIONS",
)


class EmulatorError(RuntimeError):
    pass


def _android_bin() -> str:
    exe = shutil.which("android")
    if not exe:
        raise EmulatorError("`android` CLI not found on PATH (install the Android CLI)")
    return exe


def _sdk_root() -> Path | None:
    """Resolve the Android SDK root: env → `android info` → macOS/Linux defaults."""
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v and Path(v).is_dir():
            return Path(v)
    try:
        out = _run([_android_bin(), "info"], timeout=30, check=False).stdout
        for line in out.splitlines():
            if line.lower().startswith("sdk:"):
                p = Path(line.split(":", 1)[1].strip())
                if p.is_dir():
                    return p
    except Exception:
        pass
    for default in (
        Path.home() / "Library" / "Android" / "sdk",
        Path.home() / "Android" / "Sdk",
    ):
        if default.is_dir():
            return default
    return None


def _adb_bin() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    root = _sdk_root()
    if root:
        cand = root / "platform-tools" / "adb"
        if cand.is_file():
            return str(cand)
    raise EmulatorError(
        "`adb` not found on PATH, under ANDROID_HOME/platform-tools, or via `android info`"
    )


# Default ceiling so a wedged adb/android daemon can't block a tool or e2e loop
# forever. Long ops (install/start) pass their own larger explicit timeout.
_DEFAULT_TIMEOUT_S = 60.0


def _run(
    cmd: list[str], *, timeout: float | None = _DEFAULT_TIMEOUT_S, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise EmulatorError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise EmulatorError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"  stdout: {proc.stdout.strip()}\n  stderr: {proc.stderr.strip()}"
        )
    return proc


def android(
    *args: str, timeout: float | None = _DEFAULT_TIMEOUT_S, check: bool = True
) -> subprocess.CompletedProcess:
    return _run([_android_bin(), *args], timeout=timeout, check=check)


def adb(
    *args: str,
    serial: str | None = None,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
    check: bool = True,
) -> subprocess.CompletedProcess:
    pre = [_adb_bin()]
    if serial:
        pre += ["-s", serial]
    return _run([*pre, *args], timeout=timeout, check=check)


# ---------------------------------------------------------------------------
# Knowledge + project introspection (android CLI; no device needed)
# ---------------------------------------------------------------------------
def docs_search(query: str, *, timeout: float = 90.0) -> str:
    """Search the Android Knowledge Base (`android docs search`). Returns ranked hits.

    Grounded, authoritative Android/Compose/API guidance — use instead of guessing
    when debugging the app or authoring a UI journey. First call may build the KB index.
    """
    return android("docs", "search", query, timeout=timeout).stdout.strip()


def docs_fetch(url: str, *, timeout: float = 90.0) -> str:
    """Fetch a Knowledge Base article by its `kb://...` URL (`android docs fetch`)."""
    return android("docs", "fetch", url, timeout=timeout).stdout.strip()


def describe_project(project_dir: str | Path, *, timeout: float = 120.0) -> str:
    """`android describe` — project metadata incl. build targets + APK output paths (JSON).

    The authoritative way to locate a build's artifacts (vs globbing `build/outputs/apk`).
    """
    return android("describe", "--project_dir", str(project_dir), timeout=timeout).stdout.strip()


def version_lookup(query: str, *, timeout: float = 60.0) -> str:
    """`android studio version-lookup` — latest maven artifact / Android / tool versions."""
    return android("studio", "version-lookup", query, timeout=timeout).stdout.strip()


def render_compose_preview(
    file: str | Path,
    *,
    preview: str | None = None,
    out: str | Path | None = None,
    timeout: float = 180.0,
) -> str:
    """`android studio render-compose-preview` — render a @Preview to a PNG, no emulator.

    Fast Compose UI regression / screenshot-diff. Needs a running Android Studio (the CLI talks
    to it); `android studio check` reports instance status. Returns the CLI output (the PNG path).
    """
    args = ["studio", "render-compose-preview", str(file)]
    if preview:
        args += ["--preview", preview]
    if out:
        args += ["--output", str(out)]
    return android(*args, timeout=timeout).stdout.strip()


# ---------------------------------------------------------------------------
# Device-type detection
# ---------------------------------------------------------------------------
def is_emulator(serial: str) -> bool:
    """True when `serial` identifies an AVD (emulator-XXXX), False for physical."""
    return serial.startswith("emulator-")


def list_devices() -> list[tuple[str, str]]:
    """Return [(serial, state)] for all connected adb devices (emulators + physical)."""
    out = adb("devices").stdout
    result = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] in ("device", "offline", "unauthorized"):
            result.append((parts[0], parts[1]))
    return result


def find_device_serial(*, physical_only: bool = False, emulator_only: bool = False) -> str | None:
    """Return the serial of the first ready adb device matching the filter.

    Args:
        physical_only: skip emulators (USB-attached phones only).
        emulator_only: skip physical devices (AVDs only).
    """
    for serial, state in list_devices():
        if state != "device":
            continue
        if physical_only and is_emulator(serial):
            continue
        if emulator_only and not is_emulator(serial):
            continue
        return serial
    return None


# ponytail: kept for callers that already use this name
def first_emulator_serial() -> str | None:
    """Return the serial of the first ready AVD. Use find_device_serial() for new code."""
    return find_device_serial(emulator_only=True)


# ---------------------------------------------------------------------------
# AVD lifecycle (emulator-only)
# ---------------------------------------------------------------------------
def list_avds() -> list[str]:
    out = android("emulator", "list").stdout.strip()
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def create_avd(profile: str = "medium_phone") -> None:
    """Create an AVD from a device profile (see `android emulator create --list-profiles`)."""
    android("emulator", "create", profile)


def start(avd: str, *, cold: bool = False, timeout: float = 300) -> None:
    """Launch an AVD. `android emulator start` blocks until the device is ready."""
    args = ["emulator", "start"]
    if cold:
        args.append("--cold")
    args.append(avd)
    android(*args, timeout=timeout)


def stop(avd: str | None = None) -> None:
    android(*(["emulator", "stop"] + ([avd] if avd else [])), check=False)


def ensure_avd(profile: str = "medium_phone", name: str | None = None) -> str:
    """Return an available AVD name, creating one from `profile` if none match."""
    avds = list_avds()
    if name and name in avds:
        return name
    if not name and avds:
        return avds[0]
    create_avd(profile)
    avds = list_avds()
    if not avds:
        raise EmulatorError(f"AVD creation from profile {profile!r} produced no devices")
    return name if (name and name in avds) else avds[0]


# ---------------------------------------------------------------------------
# Device readiness + app deploy
# ---------------------------------------------------------------------------
def wait_for_boot(serial: str | None = None, timeout: float = 180) -> str:
    """Block until the device reports sys.boot_completed=1; return its serial.

    Works for both emulators (AVDs) and physical devices.  For physical devices
    that are already running, this returns almost immediately.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ser = serial or find_device_serial()
        if ser:
            r = adb("shell", "getprop", "sys.boot_completed", serial=ser, check=False)
            if r.stdout.strip() == "1":
                return ser
        time.sleep(2)
    raise EmulatorError(f"no booted device within {timeout}s")


def install_app(apks: list[str] | str, *, serial: str | None = None) -> None:
    """Deploy an APK to the target device.

    Emulator: uses ``android run --apks`` (handles split APKs + launch).
    Physical device: uses ``adb install`` / ``adb install-multiple``.
    """
    paths = [apks] if isinstance(apks, str) else list(apks)
    if serial and not is_emulator(serial):
        # Physical device path: adb install / adb install-multiple.
        if len(paths) == 1:
            adb("install", "-r", paths[0], serial=serial, timeout=300)
        else:
            adb("install-multiple", "-r", *paths, serial=serial, timeout=300)
    else:
        # Emulator path: android run handles split APKs and launches the app.
        args = ["run", "--apks", ",".join(paths)]
        if serial:
            args += ["--device", serial]
        android(*args, timeout=300)


def is_app_installed(package: str, serial: str | None = None) -> bool:
    out = adb("shell", "pm", "list", "packages", serial=serial, check=False).stdout
    return any(line.strip() == f"package:{package}" for line in out.splitlines())


def resolve_package(serial: str | None = None) -> str | None:
    """Find whichever Meshtastic-Android applicationId is installed on the device.

    Tries `KNOWN_PACKAGES` in order (fdroid debug preferred). Returns None if
    none are installed. Needed because the app ships under several
    applicationIds depending on build variant/flavor (fdroid vs google, debug
    vs release), and a single device may have more than one installed side by
    side (they don't collide — different applicationIds).
    """
    for pkg in KNOWN_PACKAGES:
        if is_app_installed(pkg, serial=serial):
            return pkg
    return None


# ---------------------------------------------------------------------------
# Fresh-install launch (skip onboarding + pre-grant permissions)
# ---------------------------------------------------------------------------
def grant_runtime_permissions(
    package: str | None = None,
    *,
    serial: str | None = None,
    permissions: Sequence[str] = ONBOARDING_PERMISSIONS,
) -> None:
    """Pre-grant the app's runtime permissions via `pm grant` (native, no app code).

    Pre-accepts the permission dialogs the app would otherwise raise during
    onboarding, so a fresh-install automation run isn't blocked on them. Each
    grant is best-effort (check=False): POST_NOTIFICATIONS only exists on API
    33+, and a permission the manifest doesn't declare errors out — neither
    should abort the rest. Pairs with `launch_app(skip_onboarding=True)`.
    """
    pkg = package or resolve_package(serial=serial)
    if not pkg:
        return
    for perm in permissions:
        adb("shell", "pm", "grant", pkg, perm, serial=serial, check=False)


def launch_app(
    package: str | None = None,
    *,
    serial: str | None = None,
    skip_onboarding: bool = False,
    activity: str = MAIN_ACTIVITY,
) -> None:
    """Launch the app's main activity via `am start -n <pkg>/<activity>`.

    `skip_onboarding=True` adds the debug-only `--ez skip_onboarding true` extra
    (Meshtastic-Android#6044), which calls onAppIntroCompleted() so a fresh
    install lands straight on the main Connection screen instead of the intro
    flow. That extra is BuildConfig.DEBUG-gated in the app — a harmless no-op on
    release/Play builds or on app versions predating the PR (they just show the
    intro, and the UI-tap 'Skip' fallback in connect_app_to_tcp still handles
    it). Pre-grant the permission dialogs with grant_runtime_permissions() for a
    fully hands-free fresh-install bring-up.
    """
    pkg = package or resolve_package(serial=serial)
    if not pkg:
        raise EmulatorError("no Meshtastic app installed to launch (see resolve_package)")
    args = ["shell", "am", "start", "-n", f"{pkg}/{activity}"]
    if skip_onboarding:
        args += ["--ez", EXTRA_SKIP_ONBOARDING, "true"]
    adb(*args, serial=serial, check=False)


def prepare_fresh_install(
    package: str | None = None,
    *,
    serial: str | None = None,
) -> None:
    """One call to make a freshly-installed app automation-ready.

    Pre-grants runtime permissions, then launches skipping onboarding — the
    hands-free equivalent of clicking through the intro + accepting every
    permission dialog. After this, drive the app normally (e.g.
    connect_app_to_tcp / deeplink). Both steps are best-effort and degrade
    gracefully on app builds predating Meshtastic-Android#6044 (the UI-tap
    onboarding-skip in connect_app_to_tcp remains the fallback).
    """
    grant_runtime_permissions(package, serial=serial)
    launch_app(package, serial=serial, skip_onboarding=True)


# ---------------------------------------------------------------------------
# Deep links (fast, version-resilient navigation — prefer over UI taps)
# ---------------------------------------------------------------------------
def deeplink(path: str, *, serial: str | None = None, package: str | None = None) -> None:
    """Fire a Meshtastic-Android deep link via `am start -a VIEW`.

    `path` is the part after the host, e.g. `connections?address=t192.168.1.1:4403`,
    `nodes`, `map/3`, `settings/helpDocs`. See
    docs/en/developer/navigation-and-deep-links.md (or `DeepLinkRouter.route()`
    KDoc in the app source) for the full, current list of supported paths.

    Targets the resolved package explicitly (`resolve_package()` if not given)
    so the intent lands even when the app isn't already foregrounded. Safe to
    call before the app has completed first-run onboarding: the app applies a
    pending deep-link route once onboarding clears rather than dropping it
    (verified live against 2.8.0 fdroid debug, 2026-07-01) — no need to dismiss
    onboarding screens first.
    """
    pkg = package or resolve_package(serial=serial)
    uri = f"{DEEPLINK_SCHEME}/{path.lstrip('/')}"
    args = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri]
    if pkg:
        args.append(pkg)
    adb(*args, serial=serial, check=False)


def connect_app_via_deeplink(
    address: str, *, serial: str | None = None, package: str | None = None
) -> None:
    """Connect the app to a device by address via the `/connections?address=` deep link.

    `address` is the app's internal transport-prefixed format:
      - `t<host>:<port>` — TCP, e.g. `t192.168.1.168:4403` or `t127.0.0.1:4403`
      - `x<AA:BB:CC:DD:EE:FF>` — BLE MAC
      - `s<path>` — serial/USB, e.g. `s/dev/ttyUSB0`

    Requires Meshtastic-Android built from `main` @ 60119ce9d (2026-07-01,
    meshtastic/Meshtastic-Android#6036) or later. Older builds ignore the
    `address` param (deep link still opens the Connections screen, just
    doesn't auto-connect) — `connect_app_to_tcp()` falls back to UI taps in
    that case, so prefer that entry point unless you know the app is new
    enough.

    One shell call, no UI-tree parsing, no dependency on button positions/
    labels — use this instead of `connect_app_to_tcp`'s UI-tap fallback
    whenever the app build is known to include the address param.
    """
    deeplink(f"connections?address={address}", serial=serial, package=package)


def disconnect_app_via_deeplink(*, serial: str | None = None, package: str | None = None) -> None:
    """Disconnect the app's current device via `/connections?address=n`.

    `n` is the app's internal "no device selected" sentinel (`NO_DEVICE_SELECTED`).
    Same version requirement as `connect_app_via_deeplink`.
    """
    deeplink(f"connections?address={NO_DEVICE_SELECTED}", serial=serial, package=package)


# ---------------------------------------------------------------------------
# TCP connectivity
# ---------------------------------------------------------------------------
def adb_reverse(host_port: int, device_port: int, serial: str | None = None) -> None:
    """Set up an adb reverse tunnel: device's localhost:<device_port> → host:<host_port>.

    This is the physical-device equivalent of the emulator's 10.0.2.2 alias.
    After calling this, the app on the phone can connect to 127.0.0.1:<device_port>
    and reach the host's meshtasticd.
    """
    adb("reverse", f"tcp:{device_port}", f"tcp:{host_port}", serial=serial)


def tcp_dut_address(port: int = DEFAULT_TCP_PORT, *, serial: str | None = None) -> str:
    """Return the host:port the Meshtastic app should connect to for its DUT radio.

    Emulator: returns the AVD host-alias ``10.0.2.2:<port>`` (no tunnel needed).
    Physical device: sets up an adb reverse tunnel and returns ``127.0.0.1:<port>``.
    """
    if serial and not is_emulator(serial):
        adb_reverse(port, port, serial=serial)
        return f"127.0.0.1:{port}"
    return f"{EMULATOR_HOST_ALIAS}:{port}"


# ---------------------------------------------------------------------------
# UI observation — device-type-aware
# ---------------------------------------------------------------------------
def _parse_uiautomator_xml(xml_text: str) -> list[dict[str, Any]]:
    """Flatten uiautomator XML into the same dict schema as `android layout` JSON.

    Schema per element: text, interactions (list), center ([x, y]), and
    optionally content_desc / resource_id.
    """
    nodes: list[dict[str, Any]] = []

    def _walk(node: ET.Element) -> None:
        interactions = []
        if node.get("clickable") == "true" or node.get("long-clickable") == "true":
            interactions.append("clickable")
        if node.get("focusable") == "true":
            interactions.append("focusable")
        if node.get("scrollable") == "true":
            interactions.append("scrollable")

        center: list[int] | None = None
        bounds = node.get("bounds", "")
        if bounds:
            # Allow negative coords: partially off-screen views report e.g.
            # "[-5,84][1080,210]". A bare \d+ would drop the leading pair and
            # silently leave the element with no tappable center.
            coords = re.findall(r"\[(-?\d+),(-?\d+)\]", bounds)
            if len(coords) == 2:
                x1, y1 = int(coords[0][0]), int(coords[0][1])
                x2, y2 = int(coords[1][0]), int(coords[1][1])
                center = [(x1 + x2) // 2, (y1 + y2) // 2]

        el: dict[str, Any] = {
            "text": node.get("text", ""),
            "interactions": interactions,
        }
        if center is not None:
            el["center"] = center
        if cd := node.get("content-desc", ""):
            el["content_desc"] = cd
        if rid := node.get("resource-id", ""):
            el["resource_id"] = rid
        nodes.append(el)
        for child in node:
            _walk(child)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        # Raise (don't return []) so a malformed/partial dump is distinguishable
        # from a genuinely empty screen — poll_for_text's except EmulatorError
        # then retries the transient mid-animation case, matching the emulator
        # (android layout) path which also raises on bad output.
        raise EmulatorError(f"uiautomator dump was not valid XML: {exc}") from exc
    for child in root:
        _walk(child)
    return nodes


def _ui_dump_physical(serial: str) -> list[dict[str, Any]]:
    """UI dump via uiautomator for physical (non-emulator) devices."""
    out = adb("exec-out", "uiautomator", "dump", "/dev/tty", serial=serial, timeout=30).stdout
    # uiautomator prepends "UI hierchary dumped to: /dev/tty\n" on some versions — strip it.
    xml_start = out.find("<?xml")
    if xml_start == -1:
        xml_start = out.find("<hierarchy")
    if xml_start > 0:
        out = out[xml_start:]
    # Some uiautomator versions (observed live on Pixel 6a / Android 17) also
    # append the same status message *after* the closing tag on the same line,
    # with no separating newline ("...</hierarchy>UI hierchary dumped to: ..."),
    # which trips ET.fromstring with "junk after document element". Truncate at
    # the true end of the XML document rather than trusting EOF.
    xml_end = out.rfind("</hierarchy>")
    if xml_end != -1:
        out = out[: xml_end + len("</hierarchy>")]
    return _parse_uiautomator_xml(out)


def ui_dump(serial: str | None = None, diff: bool = False) -> list[dict[str, Any]]:
    """Return a parsed UI element list for the target device.

    Emulator: uses ``android layout`` (supports --diff).
    Physical device: uses ``adb exec-out uiautomator dump`` (diff not supported;
    the full tree is returned and callers can diff themselves).
    """
    if serial and not is_emulator(serial):
        return _ui_dump_physical(serial)
    args = ["layout"]
    if serial:
        args += ["--device", serial]
    if diff:
        args.append("--diff")
    out = android(*args).stdout.strip()
    try:
        return json.loads(out) if out else []
    except json.JSONDecodeError as exc:
        raise EmulatorError(f"layout returned non-JSON (WebView/animation?): {exc}") from exc


def screenshot(out_path: str | Path, *, serial: str | None = None, annotate: bool = False) -> Path:
    """Capture a screenshot to `out_path`.

    Emulator: uses ``android screen capture`` (supports --annotate).
    Physical device: uses ``adb exec-out screencap -p`` (annotate ignored).
    """
    out_path = Path(out_path)
    if serial and not is_emulator(serial):
        # Write to a temp sibling and os.replace only on success, so a failed or
        # timed-out screencap never leaves a zero-byte/partial PNG for a later
        # OCR/poll consumer to read.
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        try:
            with tmp.open("wb") as fh:
                proc = subprocess.run(
                    [_adb_bin(), "-s", serial, "exec-out", "screencap", "-p"],
                    stdout=fh,
                    stderr=subprocess.PIPE,
                    timeout=_DEFAULT_TIMEOUT_S,
                )
        except subprocess.TimeoutExpired as exc:
            tmp.unlink(missing_ok=True)
            raise EmulatorError(f"screencap timed out after {_DEFAULT_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise EmulatorError(f"screencap failed: {proc.stderr.decode(errors='replace').strip()}")
        os.replace(tmp, out_path)
        return out_path
    args = ["screen", "capture", "-o", str(out_path)]
    if annotate:
        args.append("--annotate")
    if serial:
        args += ["--device", serial]
    android(*args)
    return out_path


def find_text(token: str, serial: str | None = None) -> bool:
    """True when `token` appears anywhere in the current UI tree."""
    return any(token in json.dumps(el) for el in ui_dump(serial=serial))


def tap(x: int, y: int, serial: str | None = None) -> None:
    adb("shell", "input", "tap", str(x), str(y), serial=serial)


def type_text(text: str, serial: str | None = None) -> None:
    # adb input text uses %s for spaces; keep tokens space-free (e.g. E2E-<ts>).
    adb("shell", "input", "text", text, serial=serial)


def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 400, serial: str | None = None) -> None:
    adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms), serial=serial)


def poll_for_text(
    token: str, *, serial: str | None = None, timeout: float = 30, interval: float = 1.0
) -> bool:
    """Bounded poll of the UI tree for `token` (the app-plane oracle)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if find_text(token, serial=serial):
                return True
        except EmulatorError:
            pass  # transient WebView/animation; retry
        time.sleep(interval)
    return False


def clear_logcat(*, serial: str | None = None) -> None:
    """Flush the logcat ring buffer (call before a stimulus to scope the oracle)."""
    adb("logcat", "-c", serial=serial, check=False)


def read_logcat(
    *, serial: str | None = None, tags: list[str] | None = None, grep: str | None = None
) -> str:
    """Dump the current logcat buffer (`-d`), optionally tag-filtered / grepped.

    A lightweight app-event oracle for things that surface in logs but not the
    a11y tree (notifications, background workers, lifecycle). Pair with
    `clear_logcat()` before the stimulus to scope what you read.
    """
    args = ["logcat", "-d", "-v", "brief"]
    if tags:
        args += [*[f"{t}:V" for t in tags], "*:S"]
    out = adb(*args, serial=serial, check=False, timeout=20).stdout or ""
    if grep:
        out = "\n".join(ln for ln in out.splitlines() if grep.lower() in ln.lower())
    return out


def poll_logcat(
    token: str,
    *,
    serial: str | None = None,
    timeout: float = 30,
    interval: float = 1.0,
    tags: list[str] | None = None,
) -> str | None:
    """Bounded poll of logcat for `token`; returns the first matching line or None.

    The log-based app-event oracle (e.g. notification dispatch, geofence
    enter/exit). Call `clear_logcat()` first so a prior run can't false-positive.
    """
    deadline = time.time() + timeout
    needle = token.lower()
    while time.time() < deadline:
        for ln in read_logcat(serial=serial, tags=tags).splitlines():
            if needle in ln.lower():
                return ln.strip()
        time.sleep(interval)
    return None


def poll_notification(
    token: str, *, serial: str | None = None, timeout: float = 30, interval: float = 1.0
) -> str | None:
    """Bounded poll of `dumpsys notification` for `token` (the notification oracle).

    Returns the first matching title/text line, or None. Use for features that
    surface as system notifications (geofence enter/exit, message alerts) which
    don't appear in the foreground a11y tree.
    """
    deadline = time.time() + timeout
    needle = token.lower()
    while time.time() < deadline:
        out = (
            adb(
                "shell",
                "dumpsys",
                "notification",
                "--noredact",
                serial=serial,
                check=False,
                timeout=20,
            ).stdout
            or ""
        )
        for ln in out.splitlines():
            low = ln.lower()
            if needle in low and (
                "android.title" in low or "android.text" in low or "tickertext" in low
            ):
                return ln.strip()
        time.sleep(interval)
    return None


def _find_center(predicate, *, serial: str | None = None) -> tuple[int, int] | None:
    """Return the (x, y) center of the first UI element matching `predicate(el)`."""
    for el in ui_dump(serial=serial):
        if predicate(el):
            c = el.get("center")
            if isinstance(c, str):  # "[x,y]"
                x, y = (int(v) for v in c.strip("[]").split(","))
                return x, y
            if isinstance(c, (list, tuple)) and len(c) == 2:
                return int(c[0]), int(c[1])
    return None


def _tap_text(label: str, *, serial: str | None = None, contains: bool = False) -> bool:
    def pred(el):
        t = el.get("text") or ""
        return (label.lower() in t.lower()) if contains else (t == label)

    xy = _find_center(pred, serial=serial)
    if xy:
        tap(*xy, serial=serial)
        return True
    return False


def connect_app_to_tcp(
    host: str = EMULATOR_HOST_ALIAS,
    port: int = DEFAULT_TCP_PORT,
    *,
    serial: str | None = None,
    settle_s: float = 1.5,
    confirm_timeout_s: float = 20.0,
    prefer_deeplink: bool = True,
    force_reconnect: bool = False,
) -> bool:
    """Connect the app to a TCP node at `host:port`.

    For emulators, pass host=EMULATOR_HOST_ALIAS (default).
    For physical devices, call tcp_dut_address(serial=serial) first to set up the
    adb reverse tunnel, then pass host="127.0.0.1" (or any LAN-reachable host the
    phone can already route to — the reverse tunnel is only needed when the phone
    can't otherwise reach the host, e.g. no shared Wi-Fi).

    Fast path (`prefer_deeplink=True`, default): fires the `/connections?address=`
    deep link (meshtastic/Meshtastic-Android#6036, merged 2026-07-01) — one `adb
    shell am start`, no UI-tree parsing, works even mid-onboarding, version-
    resilient to layout changes. Falls back to the legacy UI-tap flow below if the
    "Not connected" label doesn't clear within `confirm_timeout_s` (covers app
    builds predating the deep link, or a slow/failed connection where the fallback
    won't help either — caller should still verify the final result via the
    device-plane oracle, e.g. `replay_status().connected`, which is authoritative;
    UI text is a best-effort signal only). `confirm_timeout_s` defaults generously
    (20s) because a real handshake + full NodeDB download over TCP visibly takes
    longer than a UI-animation settle — verified live: ~10-30s for a 20-40 node
    capture on a Pixel 6a, occasionally longer. Don't shrink this to `settle_s`-ish
    values; that just makes the (slower, less reliable) UI-tap fallback fire while
    the deep-link connect is still in flight. Note: the Connections screen's
    "Disconnect" button label is present regardless of connection state — don't
    use it as a connected signal, use the absence of "Not connected" instead
    (what this does).

    **Sticky address / auto-reconnect** (verified live 2026-07-01): once the app
    has an address configured, it keeps retrying that address indefinitely on its
    own — a dropped TCP session (e.g. the DUT/replay session restarting) gets
    silently reconnected with no further deep link needed. The converse: re-firing
    `?address=` with the *same* value the app is already using/retrying may be a
    Compose no-op (`LaunchedEffect(key.address)` doesn't restart for an unchanged
    key) — you can't tell from that call alone whether it did anything. Only an
    explicit `/connections?address=n` disconnect (`disconnect_app_via_deeplink`)
    stops the retries. Pass `force_reconnect=True` to disconnect first, guaranteeing
    a fresh, observable handshake even when reconnecting to the same address
    (e.g. after swapping which DUT/replay session is listening on that port).

    Legacy UI-tap flow (`prefer_deeplink=False`, or deep-link confirm timeout):
    codifies the navigation validated live 2026-06-24 against Meshtastic-Android 2.8:
      onboarding Skip ×3 → Connection screen → ensure TCP transport on →
      "Add device manually…" → type address (Port defaults to 4403) → Add.
    Best-effort + UI-driven; layout can shift between app versions, so callers should
    verify the result (e.g. `poll_for_text("Disconnect")` or the node short name).

    Returns True if a connect action was issued (deep link fired, or the UI Add
    action was issued) — not a guarantee of an established connection either way.
    """
    if prefer_deeplink:
        if force_reconnect:
            disconnect_app_via_deeplink(serial=serial)
            time.sleep(settle_s)
        connect_app_via_deeplink(f"t{host}:{port}", serial=serial)
        deadline = time.monotonic() + confirm_timeout_s
        while time.monotonic() < deadline:
            try:
                if not find_text("Not connected", serial=serial):
                    return True
            except EmulatorError:
                pass  # transient dump failure (mid-animation) — keep polling
            time.sleep(0.5)
        # Fall through to the UI-tap flow — either an old app build ignoring
        # `?address=`, or a still-in-flight/failed connection the caller must
        # verify regardless. Cheap to attempt; a harmless no-op if already wired.
    # 1. Dismiss onboarding permission pages (BLE/Location/Notifications) — Skip up to 4x.
    for _ in range(4):
        if not _tap_text("Skip", serial=serial):
            break
        time.sleep(settle_s)
    # 2. Ensure the TCP transport section is shown (the pill toggles the Network section).
    if not _find_center(
        lambda e: "add device manually" in (e.get("text") or "").lower(), serial=serial
    ):
        _tap_text("TCP", serial=serial)
        time.sleep(settle_s)
    # 3. Open the manual add dialog.
    if not _tap_text("Add device manually", serial=serial, contains=True):
        return False
    time.sleep(settle_s)
    # 4. Type the address into the focusable field above the (prefilled) port box.
    field = _find_center(
        lambda e: "focusable" in (e.get("interactions") or []) and not (e.get("text") or ""),
        serial=serial,
    )
    if field:
        tap(*field, serial=serial)
        time.sleep(0.5)
        type_text(host, serial=serial)
        time.sleep(0.5)
    # 5. Confirm.
    adb("shell", "input", "keyevent", "111", serial=serial, check=False)  # close keyboard
    time.sleep(0.5)
    return _tap_text("Add", serial=serial)
