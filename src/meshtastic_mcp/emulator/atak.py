# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ATAK-on-emulator fleet orchestration for CoT capture and multi-node e2e.

Stands up N Android emulators, each running ATAK-CIV connected to a
:class:`~meshtastic_mcp.replay.cot_relay.CotRelay`, so an agent can capture real
CoT shapes and drive multiple TAK nodes around a map — headless and repeatable.

Why emulators (not the physical phone) are the primary node:

* ``adb emu geo fix`` feeds ATAK a *genuine* GPS fix. Real devices reject
  Android's mock-location provider for self-position (anti-spoof), so a physical
  phone's PLI can only be moved by hand — an emulator's can be scripted.
* First-run (EULA, permission rationale, all-files access, device setup) is
  fully scriptable; no Play Store install or manual taps.

Design, folding in the fleet-orchestration research:

* **Per-clone AVDs**, not ``-read-only`` shares: each instance owns its userdata
  qcow2 so it can hold a provisioned snapshot. Clone = copy ``<base>.avd`` +
  rewrite the ini paths/id.
* **Explicit even ports** (5554, 5556, …): console=N, adb=N+1, gRPC=N+3000. We
  socket-probe a port before claiming it and always address ``adb -s`` — never
  bare ``adb devices``.
* **Provision once, snapshot, restore.** Cold-boot → install ATAK → grant perms
  → stage the defaults → walk first-run → quiesce → ``adb emu avd snapshot
  save provisioned``. Later boots load that snapshot (``-no-snapshot-save``), turning
  a ~15 min setup into a ~60 s bring-up. The snapshot is keyed on
  (APK sha, system-image build) so a stale one is re-provisioned, not errored.
* **Headless footprint flags** and a real boot health check
  (``sys.boot_completed`` + ``init.svc.bootanim`` stopped or unstarted), matching what the CI
  emulator actions wait on.

``adb emu geo fix`` takes **longitude first** — a silent wrong-hemisphere trap
(surfaced by iksnerd/adb-mcp). :func:`set_position` takes ``(lat, lon)`` and
swaps internally so callers use map order.

Android capability (needs ``adb`` + the ``emulator`` binary). ATAK first-run
dialog automation is best-effort and version-sensitive; the render/capture
assertion is the real gate.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import math
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import avd

ATAK_PACKAGE = "com.atakmap.app.civ"
FLEET_NODE_PREFIX = "atak-node-"
ATAK_ACTIVITY = "com.atakmap.app.ATAKActivity"

# Runtime permissions ATAK needs; pre-granting pre-accepts its dialogs. Superset
# of what we validated by hand this session (location incl. background, camera,
# mic, notifications, media, phone state). MANAGE_EXTERNAL_STORAGE is NOT here:
# it is an appops special-access grant, handled separately in provision().
ATAK_PERMISSIONS = (
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.READ_PHONE_STATE",
    "android.permission.READ_MEDIA_IMAGES",
    "android.permission.READ_MEDIA_VIDEO",
    "android.permission.READ_MEDIA_AUDIO",
)

# First-run dialog buttons, walked in order. Best-effort: each is tapped only if
# its label is present, so ordering/version drift just skips a step.
_FIRST_RUN_LABELS = ("I agree.", "I understand", "Allow", "I understand", "Done", "OK", "Allow")

# Headless fleet launch flags (the CI-action consensus set). KVM accel stays on
# (this is a Linux workstation); swiftshader avoids GPU-passthrough instability.
_FLEET_FLAGS = (
    "-no-window",
    "-gpu",
    "swiftshader_indirect",
    "-noaudio",
    "-no-boot-anim",
    "-no-metrics",
    # The Play image runs GMS dex2oat + Play Store after boot; at 2 cores / 2 GB
    # the emulator ANRs through ATAK's first run.
    "-cores",
    "3",
    "-memory",
    "4096",
    # ATAK-CIV is ~108 MB; the default 6 GB userdata is ~94% full out of the box.
    "-partition-size",
    "8192",
)

# adb emu geo fix defaults GPS to (0,0); provision() sets a real fix so ATAK
# accepts the position (no "NO GPS" state). Overridable per node.
_DEFAULT_FIX = (41.6070, -93.7690)  # (lat, lon) — central US

# ATAK's network/comms logging tag. A logcat filter of "<tag>:V *:S" shows only
# the streaming-connection lines (connect / retry / rx) and silences everything
# else — chiefly the emulator's emuglGLESv2_enc GL-error flood, which otherwise
# buries every useful line. This is the single source of truth for that filter.
ATAK_LOG_TAG = "CommsMapComponentCommo"


def logcat_argv(serial: str, *, follow: bool = True) -> list[str]:
    """adb argv for a clean ATAK-comms logcat on ``serial`` (only ``ATAK_LOG_TAG``,
    everything else silenced). ``follow`` streams (`-v time`); else one-shot (`-d`)."""
    mode = [] if follow else ["-d"]
    return [
        "adb",
        "-s",
        serial,
        "logcat",
        *mode,
        "-v",
        "time",
        f"{ATAK_LOG_TAG}:V",
        "*:S",
    ]


class AtakError(RuntimeError):
    pass


def _emulator_bin() -> str:
    """Resolve the raw ``emulator`` binary (needed for -port/-snapshot flags the
    ``android emulator start`` wrapper does not pass through)."""
    exe = shutil.which("emulator")
    if exe:
        return exe
    root = avd._sdk_root()
    if root:
        cand = root / "emulator" / "emulator"
        if cand.is_file():
            return str(cand)
    raise AtakError("`emulator` binary not found on PATH or under ANDROID_HOME/emulator")


def _avd_home() -> Path:
    return Path.home() / ".android" / "avd"


# ---------------------------------------------------------------------------
# AVD cloning + port allocation
# ---------------------------------------------------------------------------
def clone_avd(base: str, dest: str) -> None:
    """Clone ``<base>`` into a new AVD ``<dest>`` (copy .avd dir + rewrite ini).

    Idempotent: a pre-existing ``<dest>`` is left as-is (so a re-run reuses the
    already-provisioned clone rather than wiping it).
    """
    home = _avd_home()
    base_ini, dest_ini = home / f"{base}.ini", home / f"{dest}.ini"
    base_dir, dest_dir = home / f"{base}.avd", home / f"{dest}.avd"
    if dest_dir.is_dir() and dest_ini.is_file():
        return
    if not base_dir.is_dir() or not base_ini.is_file():
        raise AtakError(f"base AVD {base!r} not found under {home}")

    shutil.copytree(base_dir, dest_dir)
    # Top-level ini: point path= at the new dir.
    text = base_ini.read_text().splitlines()
    out = []
    for line in text:
        if line.startswith("path="):
            out.append(f"path={dest_dir}")
        elif line.startswith("path.rel="):
            out.append(f"path.rel=avd/{dest}.avd")
        else:
            out.append(line)
    dest_ini.write_text("\n".join(out) + "\n")
    # config.ini: rewrite the AvdId / display name so the two are distinguishable.
    cfg = dest_dir / "config.ini"
    if cfg.is_file():
        lines = []
        for line in cfg.read_text().splitlines():
            if line.startswith("AvdId="):
                lines.append(f"AvdId={dest}")
            elif line.startswith("avd.ini.displayname="):
                lines.append(f"avd.ini.displayname={dest}")
            else:
                lines.append(line)
        cfg.write_text("\n".join(lines) + "\n")
    # Strip state carried over from a base AVD that has been booted: *.lock files
    # (else the clone refuses to boot as "another instance is running"),
    # hardware-qemu.ini / emulator-user.ini (absolute paths + stale runtime
    # state), and any snapshots (so the clone cold-boots and we own its snapshot
    # namespace). Each regenerates on first boot.
    for junk in ("hardware-qemu.ini", "emulator-user.ini"):
        (dest_dir / junk).unlink(missing_ok=True)
    for lock in dest_dir.rglob("*.lock"):
        # A lock may be a file or a dir (e.g. userdata-qemu.img.lock/ with a pid).
        if lock.is_dir():
            shutil.rmtree(lock, ignore_errors=True)
        else:
            lock.unlink(missing_ok=True)
    shutil.rmtree(dest_dir / "snapshots", ignore_errors=True)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def alloc_console_port(index: int) -> int:
    """Even console port for fleet node ``index`` (adb = port+1). Probes upward
    from 5554 so a busy port from a stale emulator is skipped, not collided."""
    port = 5554 + index * 2
    while port <= 5680 and not (_port_free(port) and _port_free(port + 1)):
        port += 2
    if port > 5680:
        raise AtakError("no free emulator console port in 5554-5680")
    return port


# ---------------------------------------------------------------------------
# Boot + health
# ---------------------------------------------------------------------------
def boot(
    avd_name: str,
    port: int,
    *,
    snapshot: str | None = None,
    wipe: bool = False,
    extra_flags: tuple[str, ...] = _FLEET_FLAGS,
    timeout: float = 300.0,
) -> str:
    """Launch ``avd_name`` on ``port`` detached; wait for full boot; return serial.

    ``snapshot`` loads a named snapshot and does NOT save on exit (hermetic
    restore). ``wipe`` factory-resets this boot. The two are mutually exclusive.
    """
    if snapshot and wipe:
        raise AtakError("snapshot and wipe are mutually exclusive")
    serial = f"emulator-{port}"
    args = [_emulator_bin(), "-avd", avd_name, "-port", str(port), *extra_flags]
    if snapshot:
        args += ["-snapshot", snapshot, "-no-snapshot-save"]
    elif wipe:
        args += ["-no-snapshot-load", "-wipe-data"]
    else:
        args += ["-no-snapshot-load"]
    # Detached so the emulator outlives this call.
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for_boot(serial, timeout=timeout)
    except Exception:
        # The emulator process is already running; a boot-health failure here
        # would otherwise orphan it (fleet_up only records nodes that booted, so
        # its cleanup can't reach this one). Kill it before re-raising. Suppress
        # errors from BOTH cleanup calls: avd.adb converts a timeout to
        # EmulatorError even with check=False, and letting that (or a terminate()
        # error) propagate would replace the original boot failure and skip the
        # rest of the cleanup — re-leaking the very process this guards against.
        with contextlib.suppress(Exception):
            avd.adb("emu", "kill", serial=serial, check=False)
        with contextlib.suppress(Exception):
            proc.terminate()
        raise
    return serial


def wait_for_boot(serial: str, *, timeout: float = 300.0) -> None:
    """Block until ``serial`` reports ``sys.boot_completed=1`` AND the boot
    animation is stopped or unstarted — the health check the CI emulator actions use."""
    deadline = time.time() + timeout
    avd.adb("wait-for-device", serial=serial, timeout=timeout, check=False)
    while time.time() < deadline:
        booted = avd.adb(
            "shell", "getprop", "sys.boot_completed", serial=serial, check=False
        ).stdout.strip()
        anim = avd.adb(
            "shell", "getprop", "init.svc.bootanim", serial=serial, check=False
        ).stdout.strip()
        # `-no-boot-anim` (a fleet flag) leaves the bootanim service unstarted,
        # so the prop is empty rather than "stopped".
        if booted == "1" and anim in ("stopped", ""):
            return
        time.sleep(2)
    raise AtakError(f"{serial} did not finish booting within {timeout}s")


# ---------------------------------------------------------------------------
# GPS / movement (emulator console)
# ---------------------------------------------------------------------------
def set_position(serial: str, lat: float, lon: float, *, speed_mps: float | None = None) -> None:
    """Set the emulator's GPS fix. Args are (lat, lon) in map order; the console
    `geo fix` wants longitude first, swapped here.

    ``speed_mps`` rides along as geo fix's optional velocity (knots) — ATAK
    reads ``Location.getSpeed()`` and never derives speed from successive
    fixes, so without it every PLI reports ``speed="0.0"``. Bearing has no
    geo fix parameter (and ``geo nmea`` is ignored by current emulators), so
    ``<track course>`` still reflects the virtual compass, not the route.
    """
    args = ["emu", "geo", "fix", f"{lon:.7f}", f"{lat:.7f}"]
    if speed_mps is not None:
        args += ["280", "8", f"{speed_mps * 1.943844:.1f}"]  # alt m, satellites, knots
    avd.adb(*args, serial=serial, check=False)


def set_battery(serial: str, percent: int) -> None:
    """Set the emulated battery level (0-100). ATAK stamps it into PLI
    ``<status battery=...>`` — real variance for the capture corpus."""
    pct = max(0, min(100, percent))
    avd.adb("emu", "power", "capacity", str(pct), serial=serial, check=False)


def _interp(
    a: tuple[float, float], b: tuple[float, float], steps: int
) -> list[tuple[float, float]]:
    return [
        (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps) for i in range(steps)
    ]


def _leg_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    mlat = 111_320.0
    mlon = 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((b[0] - a[0]) * mlat, (b[1] - a[1]) * mlon)


def drive_route(
    serial: str,
    waypoints: list[tuple[float, float]],
    *,
    speed_mps: float = 10.0,
    step_s: float = 2.0,
    stop_event: threading.Event | None = None,
) -> None:
    """Feed a moving GPS track along ``waypoints`` (each ``(lat, lon)``).

    Emits a fix every ``step_s`` seconds, spacing points so ground speed is
    ~``speed_mps``, and stamps that speed on each fix so ATAK's PLI carries
    it (ATAK does not derive speed from consecutive positions; course still
    comes from the virtual compass — see ``set_position``).
    Blocks until the route completes or ``stop_event`` is set. Emulator-only —
    a physical device rejects mock location for self-PLI.
    """
    if len(waypoints) < 2:
        raise AtakError("drive_route needs at least 2 waypoints")
    # Guard the interpolation denominator: a non-positive or non-finite
    # speed/step (caller-controlled through the MCP tool) would clamp to 1e-6
    # and spawn ~a billion points for a normal leg — an OOM. Reject up front.
    for name, val in (("speed_mps", speed_mps), ("step_s", step_s)):
        if not math.isfinite(val) or val <= 0:
            raise AtakError(f"{name} must be a positive, finite number (got {val!r})")
    for a, b in itertools.pairwise(waypoints):
        dist = _leg_meters(a, b)
        steps = max(1, int(dist / max(speed_mps * step_s, 1e-6)))
        for lat, lon in _interp(a, b, steps):
            if stop_event is not None and stop_event.is_set():
                return
            set_position(serial, lat, lon, speed_mps=speed_mps)
            time.sleep(step_s)
    set_position(serial, *waypoints[-1], speed_mps=0.0)


# ---------------------------------------------------------------------------
# Provisioning (install + perms + stream pref + first-run walk)
# ---------------------------------------------------------------------------
# ATAK's default SharedPreferences file (``PreferenceControl.DEFAULT_PREFERENCES_NAME``
# = package + "_preferences"); the ``<preference name>`` that hint/beacon keys live in.
DEFAULT_PREFS_NAME = f"{ATAK_PACKAGE}_preferences"

# Every ATAK start re-raises these unless the ``atak.hint.<id>`` pref is false
# (``HintDialogHelper.showHint``): the "Disabling Battery Optimization" hint
# (``DozeManagement``, whose postHint also launches the system "Let app always
# run in background?" dialog), "On Screen Hints" (``TextContainer``) and
# "Point Dropper Hints" (``EnterLocationDropDownReceiver``). All three sit over
# the map and break scripted UI driving.
QUIET_PREFS: dict[str, object] = {
    "atak.hint.batoptimization.issue": False,
    "atak.hint.textContainer.osd": False,
    "atak.hint.iconset": False,
}

# The 911 beacon (``EmergencyConstants.PREFERENCES_KEY_BEACON_ENABLED``) is
# re-announced on every start while this pref is true.
BEACON_OFF_PREFS: dict[str, object] = {"plugins.emergency.beacon.enabled": False}

# Pinned SPI / Digital Pointer markers persist through the StateSaver
# (``SpiButtonTool.sendCot`` → ``Marker.persist``) into this SQLCipher DB, which
# a non-rooted emulator can only delete — the same file ATAK's own
# "Clear Content" removes. Sidecars are listed so no journal outlives it.
_STATESAVER_DB = "/sdcard/atak/Databases/statesaver2.sqlite"
_STATESAVER_FILES = (
    _STATESAVER_DB,
    f"{_STATESAVER_DB}-journal",
    f"{_STATESAVER_DB}-wal",
    f"{_STATESAVER_DB}-shm",
)

# Only this path (no extension) is auto-loaded: ``PreferenceControl.ingestDefaults``
# reads it once at ``ATAKActivity`` start, then deletes it.
_DEFAULTS_PATH = "/sdcard/atak/config/prefs/defaults"


def _pref_class(value: object) -> tuple[str, str]:
    if isinstance(value, bool):
        return "java.lang.Boolean", "true" if value else "false"
    if isinstance(value, int):
        return "java.lang.Integer", str(value)
    if isinstance(value, str):
        return "java.lang.String", value
    raise AtakError(f"unsupported pref value type {type(value).__name__}")


def prefs_xml(groups: dict[str, dict[str, object]]) -> str:
    """ATAK ``.pref`` XML: ``{<preference name>: {key: value}}``. Python bool /
    int / str map to the Java ``class`` attribute ATAK's loader switches on."""
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<preferences>",
    ]
    for name, entries in groups.items():
        out.append(f'  <preference version="1" name="{name}">')
        for key, value in entries.items():
            cls, text = _pref_class(value)
            out.append(f'    <entry key="{key}" class="class {cls}">{text}</entry>')
        out.append("  </preference>")
    out.append("</preferences>")
    return "\n".join(out) + "\n"


def stream_entries(host: str, port: int, *, name: str = "cotcapture") -> dict[str, object]:
    """The ``cot_streams`` entries for one plain-TCP streaming input at host:port."""
    return {
        "count": 1,
        "description0": name,
        "connectString0": f"{host}:{port}:tcp",
        "enabled0": True,
        "useAuth0": False,
    }


def stream_pref(host: str, port: int, *, name: str = "cotcapture") -> str:
    """An ATAK ``cot_streams`` .pref (plain-TCP streaming input) at host:port."""
    return prefs_xml({"cot_streams": stream_entries(host, port, name=name)})


def push_defaults(serial: str, groups: dict[str, dict[str, object]]) -> None:
    """Stage ``groups`` (see :func:`prefs_xml`) as ATAK's ``config/prefs/defaults``,
    applied on the next ATAK start. Overwrites any not-yet-ingested file."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pref", delete=False) as fh:
        fh.write(prefs_xml(groups))
        local = fh.name
    try:
        avd.adb("push", local, _DEFAULTS_PATH, serial=serial, check=False)
    finally:
        Path(local).unlink(missing_ok=True)  # don't leave the temp pref on the host


def push_stream_pref(serial: str, host: str, port: int, *, name: str = "cotcapture") -> None:
    """Stage just the streaming-input pref (see :func:`push_defaults`)."""
    push_defaults(serial, {"cot_streams": stream_entries(host, port, name=name)})


def quiesce(serial: str) -> None:
    """Force-stop ATAK and clear the state that would otherwise self-sustain
    across its next start: the StateSaver DB (pinned SPI / Digital Pointer,
    re-sent every 5 s forever) and the 911 beacon pref.

    Deleting the StateSaver drops *every* persisted map item, which is the
    point for a scripted fleet node. The beacon pref can only be reached via
    ``defaults``, so it is cleared on the next start rather than immediately.
    """
    avd.adb("shell", "am", "force-stop", ATAK_PACKAGE, serial=serial, check=False)
    avd.adb("shell", "rm", "-f", *_STATESAVER_FILES, serial=serial, check=False)
    push_defaults(serial, {DEFAULT_PREFS_NAME: BEACON_OFF_PREFS})


def launch(serial: str, *, timeout: float = 60.0) -> None:
    """Start ATAK's main activity and wait (best-effort) for the map toolbar."""
    avd.adb(
        "shell", "am", "start", "-n", f"{ATAK_PACKAGE}/{ATAK_ACTIVITY}", serial=serial, check=False
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_text(serial, "Tools"):
            return
        time.sleep(2)


def _has_text(serial: str, token: str) -> bool:
    """``avd.find_text`` that reads a transient bad layout dump as "not on screen"."""
    try:
        return avd.find_text(token, serial=serial)
    except avd.EmulatorError:
        return False


def _walk_first_run(serial: str, *, timeout: float = 90.0) -> None:
    """Best-effort tap through ATAK's first-run dialogs (EULA → rationale →
    permission grants → device setup → battery)."""
    deadline = time.time() + timeout
    idx = 0
    while time.time() < deadline and idx < len(_FIRST_RUN_LABELS):
        label = _FIRST_RUN_LABELS[idx]
        # A starved emulator throws an ANR dialog over the EULA; Wait, never Close app.
        if _has_text(serial, "isn't responding") and _tap_label(serial, "Wait"):
            time.sleep(1.5)
            continue
        if _tap_label(serial, label):
            idx += 1
            time.sleep(1.5)
        else:
            time.sleep(1.0)
        # Bail once the map toolbar is up (nav menu button present).
        if _has_text(serial, "Tools"):
            return


def _tap_label(serial: str, label: str) -> bool:
    # avd._find_center handles center as a [x,y] list OR "[x,y]" string (the
    # emulator `android layout` path emits the latter) — don't re-unpack inline.
    try:
        c = avd._find_center(
            lambda el: el.get("text") == label and "clickable" in el.get("interactions", []),
            serial=serial,
        )
    except avd.EmulatorError:  # transient non-JSON dump mid-animation: not on screen
        return False
    if c is None:
        return False
    avd.tap(c[0], c[1], serial=serial)
    return True


def provision(
    serial: str,
    apk_path: str,
    *,
    relay_host: str = avd.EMULATOR_HOST_ALIAS,
    relay_port: int = 8087,
    fix: tuple[float, float] = _DEFAULT_FIX,
) -> None:
    """Install ATAK, grant perms, set a GPS fix, stage the defaults (stream +
    hint suppression + beacon off), walk first-run, then :func:`quiesce` and
    relaunch so the snapshot taken next holds a clean, running ATAK.

    ``relay_host`` defaults to ``10.0.2.2`` (host loopback from inside the AVD);
    pass the LAN IP for a physical device.
    """
    if not avd.is_app_installed(ATAK_PACKAGE, serial=serial):
        avd.adb("install", "-r", apk_path, serial=serial, timeout=300)
    for perm in ATAK_PERMISSIONS:
        avd.adb("shell", "pm", "grant", ATAK_PACKAGE, perm, serial=serial, check=False)
    # MANAGE_EXTERNAL_STORAGE via appops (ATAK uses native file paths).
    avd.adb(
        "shell",
        "appops",
        "set",
        ATAK_PACKAGE,
        "MANAGE_EXTERNAL_STORAGE",
        "allow",
        serial=serial,
        check=False,
    )
    # Play Store background sync starves the headless emulator; ATAK needs none of it.
    avd.adb(
        "shell",
        "pm",
        "disable-user",
        "--user",
        "0",
        "com.android.vending",
        serial=serial,
        check=False,
    )
    # Doze whitelist: DozeManagement.checkDoze skips both the battery hint and
    # the system "run in background?" dialog once the package is exempt.
    avd.adb(
        "shell",
        "dumpsys",
        "deviceidle",
        "whitelist",
        f"+{ATAK_PACKAGE}",
        serial=serial,
        check=False,
    )
    set_position(serial, *fix)
    push_defaults(
        serial,
        {
            "cot_streams": stream_entries(relay_host, relay_port),
            DEFAULT_PREFS_NAME: {**QUIET_PREFS, **BEACON_OFF_PREFS},
        },
    )
    avd.adb("shell", "am", "force-stop", ATAK_PACKAGE, serial=serial, check=False)
    avd.adb(
        "shell", "am", "start", "-n", f"{ATAK_PACKAGE}/{ATAK_ACTIVITY}", serial=serial, check=False
    )
    time.sleep(4)
    _walk_first_run(serial)
    # First-run may already have persisted map state; snapshot a clean ATAK.
    quiesce(serial)
    launch(serial)


# ---------------------------------------------------------------------------
# Snapshot (provision-once, restore-fast)
# ---------------------------------------------------------------------------
def snapshot_save(serial: str, name: str) -> None:
    avd.adb("emu", "avd", "snapshot", "save", name, serial=serial, check=False)


def provision_tag(apk_path: str) -> str:
    """Cache-key a provisioned snapshot on the APK bytes (so a new ATAK build
    re-provisions rather than restoring a stale snapshot)."""
    h = hashlib.sha256(Path(apk_path).read_bytes()).hexdigest()[:12]
    return f"provisioned_{h}"


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------
@dataclass
class FleetNode:
    name: str
    serial: str
    port: int
    callsign: str = ""


@dataclass
class Fleet:
    nodes: list[FleetNode] = field(default_factory=list)

    def serials(self) -> list[str]:
        return [n.serial for n in self.nodes]


def fleet_up(
    count: int,
    apk_path: str,
    *,
    base_avd: str,
    relay_port: int = 8087,
    use_snapshot: bool = True,
) -> Fleet:
    """Bring up ``count`` provisioned ATAK emulators cloned from ``base_avd``.

    Each node is cloned to ``atak-node-<i>``, booted on its own port, and (if no
    usable provisioned snapshot exists) provisioned then snapshotted so the next
    ``fleet_up`` restores in ~60 s. All nodes point at the host relay via
    ``10.0.2.2:<relay_port>``.
    """
    if count < 1:
        raise AtakError("count must be >= 1")
    tag = provision_tag(apk_path)
    fleet = Fleet()
    try:
        for i in range(count):
            name = f"{FLEET_NODE_PREFIX}{i}"
            clone_avd(base_avd, name)
            port = alloc_console_port(i)
            have_snap = use_snapshot and _has_snapshot(name, tag)
            serial = boot(name, port, snapshot=tag if have_snap else None, wipe=not have_snap)
            # Record the node BEFORE provisioning so a provision failure still
            # leaves the booted emulator in the fleet for teardown.
            fleet.nodes.append(FleetNode(name=name, serial=serial, port=port))
            if not have_snap:
                provision(serial, apk_path, relay_port=relay_port)
                snapshot_save(serial, tag)
    except Exception:
        # Tear down everything that came up so a partial bring-up never orphans
        # headless emulators holding ports + AVD locks.
        fleet_down(fleet)
        raise
    return fleet


def _has_snapshot(avd_name: str, tag: str) -> bool:
    """True if AVD ``avd_name`` already holds a snapshot named ``tag``."""
    snaps_dir = _avd_home() / f"{avd_name}.avd" / "snapshots" / tag
    return snaps_dir.is_dir()


def fleet_down(fleet: Fleet, *, delete_clones: bool = False) -> None:
    """Quiesce ATAK then stop every node; optionally delete the cloned AVDs.

    A snapshot boot is ``-no-snapshot-save`` so its disk state is discarded
    anyway; quiescing matters for a node whose ATAK is restarted in place, and
    keeps teardown honest regardless of how the node was booted. Quiesce
    failures (a wedged adb) must not stop the kill.
    """
    for node in fleet.nodes:
        with contextlib.suppress(Exception):
            quiesce(node.serial)
        avd.adb("emu", "kill", serial=node.serial, check=False)
    if delete_clones:
        time.sleep(2)
        for node in fleet.nodes:
            delete_clone_avd(node.name)


def delete_clone_avd(name: str) -> None:
    """Remove a cloned AVD (dir + ini) from disk. No-op if absent."""
    shutil.rmtree(_avd_home() / f"{name}.avd", ignore_errors=True)
    (_avd_home() / f"{name}.ini").unlink(missing_ok=True)


def list_clone_avds() -> list[str]:
    """Names of every ``atak-node-*`` clone on disk (running or not)."""
    return sorted(p.stem for p in _avd_home().glob(f"{FLEET_NODE_PREFIX}*.ini"))


def discover_fleet() -> Fleet:
    """Rebuild a :class:`Fleet` from the running ``atak-node-*`` emulators.

    Stateless counterpart to the handle :func:`fleet_up` returns, so a separate
    process (the CLI) can tear down a fleet it did not start. Only emulators
    whose AVD name carries the fleet prefix are included — a foreign emulator
    on the same adb server is never touched.
    """
    fleet = Fleet()
    for serial, state in avd.list_devices():
        if state != "device" or not avd.is_emulator(serial):
            continue
        out = avd.adb("emu", "avd", "name", serial=serial, check=False, timeout=10).stdout
        name = next((ln.strip() for ln in out.splitlines() if ln.strip() not in ("", "OK")), "")
        if not name.startswith(FLEET_NODE_PREFIX):
            continue
        port = int(serial.rsplit("-", 1)[1])
        fleet.nodes.append(FleetNode(name=name, serial=serial, port=port))
    return fleet
