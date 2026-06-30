# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""iOS Simulator / macOS app-plane control for hardware-free e2e (Meshtastic-Apple).

The Apple counterpart of ``avd.py``. The device plane (``native_node.py`` UDP-multicast
mesh + the recorder) is reused verbatim; only the app plane differs.

Key simplification over Android: the **iOS Simulator shares the host network stack**, so the
app connects to ``127.0.0.1:<port>`` directly — no ``10.0.2.2`` alias. A **macOS** build is
simpler still (native, localhost, no simulator).

Tooling:
- ``xcrun simctl`` — simulator lifecycle, install/launch, screenshots (always available with Xcode).
- ``idb`` (facebook/idb) — accessibility-tree dump + tap/text input (the adb-analog). Optional;
  install with ``brew tap facebook/fb && brew trust facebook/fb &&
  brew install facebook/fb/idb-companion`` (the *cli* lives in the tap, **not** the ``companion``
  cask) plus ``pipx install --python python3.12 fb-idb`` (fb-idb breaks on Python 3.14). Without
  it, UI assertions fall back to screenshots (+ external OCR) and XCUITest. Run ``meshtastic-mcp
  doctor`` for the exact, current command on this host.

Scope: this module targets the **true iOS Simulator** (full inbound loop validated live
2026-06-25). The Meshtastic scheme embeds the Watch app, so the iOS-Simulator build needs the
watchOS SDK + a *usable* watchOS runtime; if `simctl list runtimes` hides it, the cause is
duplicate disk images (delete all + re-download once) — not a missing reboot. The build must use
ad-hoc signing that keeps entitlements (else an `INPreferences`/Siri-entitlement launch crash);
see `references/simulator-apple.md`. For the simplest path, build the **macOS (Catalyst)**
target — it needs no watchOS and no idb — but drive it with
``cliclick``/``screencapture``/AX, not ``idb`` (idb targets iOS simulators, not Catalyst). See
the ``meshtastic-e2e`` skill ``references/simulator-apple.md``.

All shell calls are bounded; UI helpers return parsed data so an agent can assert on them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# The app inside the iOS Simulator reaches host TCP via localhost (shared network stack).
SIM_HOST_ALIAS = "127.0.0.1"
DEFAULT_TCP_PORT = 4403

# Active idb_companion processes keyed by UDID (module-level so callers don't have to track them).
_companion_procs: dict[str, subprocess.Popen] = {}


class AppleSimError(RuntimeError):
    pass


def _xcrun_bin() -> str:
    exe = shutil.which("xcrun")
    if not exe:
        raise AppleSimError("`xcrun` not found — install Xcode / command-line tools")
    return exe


def _idb_bin() -> str:
    exe = shutil.which("idb")
    if not exe:
        raise AppleSimError(
            "`idb` not found — install idb_companion from the facebook tap "
            "(`brew tap facebook/fb && brew trust facebook/fb && "
            "brew install facebook/fb/idb-companion`) and the client "
            "(`pipx install --python python3.12 fb-idb`; fb-idb breaks on Python 3.14). "
            "Run `meshtastic-mcp doctor` for the exact command, or use "
            "screenshots/XCUITest for UI assertions."
        )
    return exe


# Default ceiling so a stalled idb/simctl call (idb's gRPC-to-companion path is
# a known stall source) can't block a tool or e2e loop forever. Boot/bootstatus
# pass their own larger explicit timeout.
_DEFAULT_TIMEOUT_S = 60.0


def _run(
    cmd: list[str], *, timeout: float | None = _DEFAULT_TIMEOUT_S, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AppleSimError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise AppleSimError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"  stdout: {proc.stdout.strip()}\n  stderr: {proc.stderr.strip()}"
        )
    return proc


def simctl(
    *args: str, timeout: float | None = _DEFAULT_TIMEOUT_S, check: bool = True
) -> subprocess.CompletedProcess:
    return _run([_xcrun_bin(), "simctl", *args], timeout=timeout, check=check)


def idb(
    *args: str,
    udid: str | None = None,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
    check: bool = True,
) -> subprocess.CompletedProcess:
    pre = [_idb_bin()]
    cmd = [*pre, *args]
    if udid:
        cmd += ["--udid", udid]
    return _run(cmd, timeout=timeout, check=check)


def has_idb() -> bool:
    return shutil.which("idb") is not None


# ---------------------------------------------------------------------------
# idb_companion lifecycle
# ---------------------------------------------------------------------------
def start_companion(udid: str, *, timeout: float = 15.0) -> int:
    """Start `idb_companion` for *udid*, register it with the idb daemon, return the gRPC port.

    `idb_companion` and the `idb` client communicate over gRPC. The companion prints a JSON
    line ``{"grpc_port": N, ...}`` to stdout once it is ready; we parse that, then call
    ``idb connect 127.0.0.1 <port>`` so the idb client can reach it.  Any stale entry for
    *udid* in the idb daemon is disconnected first so a re-run doesn't inherit a dead socket.
    """
    companion_bin = shutil.which("idb_companion")
    if not companion_bin:
        raise AppleSimError(
            "`idb_companion` not found. "
            "brew tap facebook/fb && brew trust facebook/fb "
            "&& brew install facebook/fb/idb-companion"
        )
    # evict any stale companion entry (silently — may not be registered)
    _run([_idb_bin(), "disconnect", udid], check=False)
    stop_companion(udid)  # kill an old process we may have started earlier

    proc = subprocess.Popen(
        [companion_bin, "--udid", udid],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    _companion_procs[udid] = proc

    # Read lines until the JSON port announcement (comes quickly, usually within 2s).
    port: int | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AppleSimError(f"idb_companion exited unexpectedly (rc={proc.returncode})")
        line = proc.stdout.readline().strip()  # type: ignore[union-attr]
        if line.startswith("{") and "grpc_port" in line:
            try:
                d = json.loads(line)
                port = int(d.get("grpc_swift_port") or d.get("grpc_port") or 0) or None
                if port:
                    break
            except (ValueError, KeyError):
                pass
    if not port:
        proc.terminate()
        _companion_procs.pop(udid, None)
        raise AppleSimError(f"idb_companion did not report a gRPC port within {timeout}s")

    # Register with the idb daemon. If connect fails, tear down the companion we
    # just spawned (mirrors the no-port cleanup above) so we don't leak a live
    # idb_companion process tracked in _companion_procs.
    try:
        _run([_idb_bin(), "connect", "127.0.0.1", str(port)])
    except AppleSimError:
        stop_companion(udid)
        raise
    return port


def stop_companion(udid: str) -> None:
    """Terminate the companion process for *udid* and disconnect from the idb daemon."""
    proc = _companion_procs.pop(udid, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _run([_idb_bin(), "disconnect", udid], check=False)


# ---------------------------------------------------------------------------
# Simulator lifecycle (xcrun simctl)
# ---------------------------------------------------------------------------
def list_simulators(available_only: bool = True) -> list[dict[str, Any]]:
    """Return simulator devices as dicts (udid, name, state, runtime)."""
    # Build args conditionally — passing "" as a positional would reach simctl
    # as an (empty) name filter rather than meaning "no filter".
    args = ["list", "devices", *(["available"] if available_only else []), "--json"]
    out = simctl(*args).stdout
    data = json.loads(out)
    sims: list[dict[str, Any]] = []
    for runtime, devices in (data.get("devices") or {}).items():
        for d in devices:
            sims.append({**d, "runtime": runtime})
    return sims


def booted_udid() -> str | None:
    for s in list_simulators(available_only=False):
        if s.get("state") == "Booted":
            return s.get("udid")
    return None


def ensure_booted(name_contains: str = "iPhone", timeout: float = 180) -> str:
    """Boot a simulator (first matching `name_contains` if none booted) and return its udid."""
    udid = booted_udid()
    if udid:
        return udid
    match = next(
        (s for s in list_simulators() if name_contains.lower() in (s.get("name") or "").lower()),
        None,
    )
    if not match:
        raise AppleSimError(f"no available simulator matching {name_contains!r}")
    udid = match["udid"]
    simctl("boot", udid, check=False)
    _run([_xcrun_bin(), "simctl", "bootstatus", udid, "-b"], timeout=timeout)
    start_companion(udid)  # register idb_companion so UI calls work immediately
    return udid


def shutdown(udid: str | None = None) -> None:
    simctl("shutdown", udid or "booted", check=False)


def install_app(app_path: str | Path, *, udid: str | None = None) -> None:
    """Install a built .app bundle into the simulator."""
    simctl("install", udid or "booted", str(app_path))


def launch(bundle_id: str, *, udid: str | None = None) -> None:
    simctl("launch", udid or "booted", bundle_id)


def is_app_installed(bundle_id: str, *, udid: str | None = None) -> bool:
    r = simctl("get_app_container", udid or "booted", bundle_id, check=False)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# UI observation (oracle) + interaction (stimulus)
# ---------------------------------------------------------------------------
def ui_dump(*, udid: str | None = None) -> list[dict[str, Any]]:
    """`idb ui describe-all` → accessibility element list (the layout-tree analog)."""
    out = idb("ui", "describe-all", "--json", udid=udid).stdout.strip()
    if not out:
        return []
    # idb emits either a JSON array or newline-delimited JSON objects.
    try:
        parsed = json.loads(out)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return [json.loads(line) for line in out.splitlines() if line.strip()]


def screenshot(out_path: str | Path, *, udid: str | None = None) -> Path:
    simctl("io", udid or "booted", "screenshot", str(out_path))
    return Path(out_path)


def _element_text(el: dict[str, Any]) -> str:
    # idb accessibility nodes expose label/title/value text fields.
    return " ".join(
        str(el.get(k, "")) for k in ("AXLabel", "AXValue", "label", "title", "value", "text")
    )


def find_text(token: str, *, udid: str | None = None) -> bool:
    return any(token in _element_text(el) for el in ui_dump(udid=udid))


def tap(x: int, y: int, *, udid: str | None = None) -> None:
    idb("ui", "tap", str(x), str(y), udid=udid)


def type_text(text: str, *, udid: str | None = None) -> None:
    idb("ui", "text", text, udid=udid)


def poll_for_text(
    token: str, *, udid: str | None = None, timeout: float = 30, interval: float = 1.0
) -> bool:
    """Bounded poll of the accessibility tree for `token` (the app-plane oracle)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if find_text(token, udid=udid):
                return True
        except AppleSimError:
            pass  # transient; retry
        time.sleep(interval)
    return False


def tcp_dut_address(port: int = DEFAULT_TCP_PORT) -> str:
    """Host-side native node address the simulator (or macOS) app connects to as its DUT.

    Same value for the iOS Simulator and a native macOS build — both reach the host over
    loopback. The app's "Add device → TCP/IP" flow is UI-driven; drive it with
    `ui_dump` + `tap`/`type_text`. See the meshtastic-e2e skill `simulator-apple.md`.
    """
    return f"{SIM_HOST_ALIAS}:{port}"
