# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""UI-tier fixtures: per-node camera lifecycle, OCR warmup, per-test frame
capture, and a `ui_home_state` autouse guard that resets to the home frame
before every test (prevents state bleed if a prior test exited inside a menu).

The tier is **parametrized per screen-bearing node** (`ui_role`): every UI test
runs once per role present on the hub (esp32s3 / t_echo / heltec_t114), driving
that node's port and asserting on the camera FleetSuite has bound to it (the
binding + rotation come from the registry DB). rak4631 has no display and is
excluded.

The camera + OCR modules live in `meshtastic_mcp/{camera,ocr}.py` (production
code, so the `capture_screen` MCP tool can share them). These fixtures wire
them into pytest + write per-test captures to `tests/ui_captures/…`.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meshtastic_mcp import admin as admin_mod
from meshtastic_mcp import camera as camera_mod
from meshtastic_mcp import ocr as ocr_mod
from meshtastic_mcp.input_events import InputEventCode

from ._screen_log import FrameEvent, get_current_frame, wait_for_frame, wait_for_reason

# Roles that carry a screen the UI tier can drive. esp32s3 (heltec-v3 SSD1306),
# t_echo (LilyGO e-ink), heltec_t114 (TFT). rak4631 is a bare WisBlock module
# with no display, so it's excluded.
UI_CAPABLE_ROLES = ("esp32s3", "t_echo", "heltec_t114")

# Per-role settle (seconds) to wait after an input event before capturing the
# camera frame. e-ink does a slow full refresh, so it needs much longer than an
# OLED/TFT for the drawn frame to actually be on the glass.
_POST_EVENT_SETTLE_S = {"t_echo": 2.0}
_DEFAULT_SETTLE_S = 0.4

# Where per-test captures land. One subdirectory per session seed, then per
# sanitized test nodeid — identical pattern to other pytest artifacts.
CAPTURES_ROOT = Path(__file__).resolve().parent.parent / "ui_captures"


def _sanitize_nodeid(nodeid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", nodeid)


def _port_of(entry: Any) -> str | None:
    return entry.get("port") if isinstance(entry, dict) else entry


def post_event_settle(role: str) -> float:
    """Seconds to wait after an input event before a camera capture, by role."""
    return _POST_EVENT_SETTLE_S.get(role, _DEFAULT_SETTLE_S)


# ---------- Per-node parametrization ---------------------------------------


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run every UI test once per screen-bearing role.

    Parametrizes `ui_role` over `UI_CAPABLE_ROLES` (the autouse fixtures pull it
    in, so the whole tier is covered). We parametrize over the full set the bench
    *defines* (so collection is stable off-bench) and skip at runtime for roles
    not physically present — mirrors the root conftest's `baked_single_role`.
    """
    if "ui_role" not in metafunc.fixturenames:
        return
    from .. import _bench

    bench_roles = set(_bench.roles())
    roles = [r for r in UI_CAPABLE_ROLES if r in bench_roles] or list(UI_CAPABLE_ROLES)
    metafunc.parametrize("ui_role", roles, ids=roles, scope="function")


@pytest.fixture
def ui_port(ui_role: str, hub_devices: dict[str, Any]) -> str:
    if ui_role not in hub_devices:
        pytest.skip(f"role {ui_role!r} not present on the hub")
    port = _port_of(hub_devices[ui_role])
    if not port:
        pytest.skip(f"{ui_role!r} has no usable port")
    return port


@pytest.fixture(scope="session")
def ui_roles_present_session(hub_devices: dict[str, Any]) -> list[str]:
    """Non-skipping list of screen-bearing roles on the hub (for session setup)."""
    return [r for r in UI_CAPABLE_ROLES if r in hub_devices]


# ---------- Per-node camera resolution (DB-backed) -------------------------


def _fleetsuite_db_path() -> Path:
    """Path to the FleetSuite registry DB that holds camera↔device bindings."""
    try:
        from meshtastic_mcp.web.db.database import default_db_path

        return default_db_path()
    except Exception:
        import os

        env = os.environ.get("MESHTASTIC_MCP_WEB_DB")
        return Path(env) if env else Path.home() / ".meshtastic_mcp" / "fleetsuite.db"


def camera_binding_for_role(role: str) -> dict[str, Any] | None:
    """Resolve the camera FleetSuite has bound to ``role``'s node, by hub slot.

    role → bench hub slot (tests/_bench.py) → the device on that slot in the
    registry → the enabled camera assigned to that device's serial. Returns
    ``{device_index, rotation, mirror}`` or None when nothing's bound (caller
    then falls back to env-var / null). Read-only; never mutates the DB.
    """
    from .. import _bench

    hp = _bench.location_hub_port(_bench.role_location(role))
    if not hp:
        return None
    db_path = _fleetsuite_db_path()
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT c.device_index, c.rotation, c.mirror "
                "FROM cameras c JOIN devices d ON c.device_serial = d.serial_number "
                "WHERE d.hub_location=? AND d.hub_port=? AND c.enabled=1 "
                "ORDER BY c.id LIMIT 1",
                (hp[0], hp[1]),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return {"device_index": row[0], "rotation": int(row[1] or 0), "mirror": bool(row[2])}


@pytest.fixture(scope="session")
def _node_cameras() -> Iterator[dict[str, camera_mod.CameraBackend]]:
    """Session cache of opened camera backends, keyed by role. Opening a cv2
    capture (with warmup) is slow, so reuse one per node across the tier."""
    cams: dict[str, camera_mod.CameraBackend] = {}
    try:
        yield cams
    finally:
        for cam in cams.values():
            try:
                cam.close()
            except Exception:
                pass


@pytest.fixture
def node_camera(
    ui_role: str, _node_cameras: dict[str, camera_mod.CameraBackend]
) -> camera_mod.CameraBackend:
    """The camera bound to the node under test, oriented per the DB rotation.

    Falls back to the env-var-configured camera (then NullBackend) when the role
    has no DB binding, so the tier still runs end-to-end without hardware.
    """
    if ui_role not in _node_cameras:
        binding = camera_binding_for_role(ui_role)
        if binding is not None:
            cam = camera_mod.get_camera(
                ui_role,
                device=binding["device_index"],
                rotation=binding["rotation"],
                mirror=binding["mirror"],
            )
        else:
            cam = camera_mod.get_camera(ui_role)
        _node_cameras[ui_role] = cam
    return _node_cameras[ui_role]


# ---------- OCR warmup -----------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _ocr_warm() -> None:
    """Pay easyocr's ~100 MB / cold-start cost ONCE per session.

    Subsequent `ocr_text()` calls hit the cached reader and return quickly.
    Swallows errors — if OCR isn't installed, warm is a no-op.
    """
    try:
        ocr_mod.warm()
    except Exception:
        pass


# ---------- Per-node firmware-log view (port-filtered) ---------------------


@pytest.fixture(autouse=True)
def node_log_lines(
    request: pytest.FixtureRequest, ui_port: str, _debug_log_buffer: Any
) -> Iterator[list[str]]:
    """A per-node view of the firmware log: only `meshtastic.log.line` events
    from the node under test's port.

    The root `_debug_log_buffer` captures EVERY port's lines unfiltered, which
    would let one node's `Screen: frame …` logs bleed into another node's test.
    We subscribe our own port-filtered handler and OVERWRITE
    `request.node._debug_log_buffer` with the filtered list, so existing tests
    (and `frame_capture`) that read that attribute transparently get the
    node-scoped view. Depends on `_debug_log_buffer` so we always run after it.
    """
    from pubsub import pub  # type: ignore[import-untyped]

    lines: list[str] = []
    lock = threading.Lock()
    want = str(ui_port)

    def handler(line: str, interface: Any = None) -> None:
        dev = getattr(interface, "devPath", None)
        # Keep lines from this node's port; keep un-attributable lines too (the
        # only interface open during a UI test is this node's transient connect).
        if dev is None or str(dev) == want:
            with lock:
                lines.append(line)

    pub.subscribe(handler, "meshtastic.log.line")
    request.node._debug_log_buffer = lines  # type: ignore[attr-defined]
    request.node._ui_node_log_handler_ref = handler  # type: ignore[attr-defined]
    try:
        yield lines
    finally:
        try:
            pub.unsubscribe(handler, "meshtastic.log.line")
        except Exception:
            pass


# ---------- Per-test capture + transcript ----------------------------------


class FrameCapture:
    """Per-test capture recorder. Created once per test via the
    `frame_capture` fixture; call with a label to snapshot the screen.
    """

    def __init__(
        self,
        cam: camera_mod.CameraBackend,
        dir_path: Path,
        lines: list[str],
        nodeid: str,
    ) -> None:
        self._cam = cam
        self._dir = dir_path
        self._lines = lines
        self._nodeid = nodeid
        self._step = 0
        self.captures: list[dict[str, Any]] = []
        self._transcript_path = dir_path / "transcript.md"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._transcript_path.write_text(
            f"# {nodeid} — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n",
            encoding="utf-8",
        )

    def __call__(self, label: str) -> dict[str, Any]:
        self._step += 1
        stem = f"{self._step:03d}-{re.sub(r'[^a-zA-Z0-9_-]+', '-', label)}"
        png_path = self._dir / f"{stem}.png"
        ocr_path = self._dir / f"{stem}.ocr.txt"

        try:
            png = self._cam.capture()
        except Exception as exc:
            png = b""
            ocr_str = f"[capture error: {exc}]"
        else:
            camera_mod.save_capture(png, png_path)
            try:
                ocr_str = ocr_mod.ocr_text(png)
            except Exception as exc:
                ocr_str = f"[ocr error: {exc}]"
            ocr_path.write_text(ocr_str or "", encoding="utf-8")

        frame = get_current_frame(self._lines)
        entry: dict[str, Any] = {
            "step": self._step,
            "label": label,
            "png_path": str(png_path) if png else None,
            "ocr_text": ocr_str,
            "frame": (
                {
                    "idx": frame.idx,
                    "name": frame.name,
                    "reason": frame.reason,
                }
                if frame is not None
                else None
            ),
        }
        self.captures.append(entry)

        with self._transcript_path.open("a", encoding="utf-8") as fh:
            frame_str = (
                f"frame {frame.idx}/{frame.count} name={frame.name} reason={frame.reason}"
                if frame is not None
                else "frame <none>"
            )
            ocr_summary = (ocr_str or "").replace("\n", " / ")[:80]
            fh.write(f"{self._step}. **{label}** — {frame_str} — OCR: `{ocr_summary}`\n")
        return entry


@pytest.fixture
def frame_capture(
    request: pytest.FixtureRequest,
    node_camera: camera_mod.CameraBackend,
    session_seed: str,
) -> Iterator[FrameCapture]:
    nodeid = _sanitize_nodeid(request.node.nodeid)
    dir_path = CAPTURES_ROOT / session_seed / nodeid
    # Fresh directory per test run so reruns don't mix old and new images.
    if dir_path.exists():
        shutil.rmtree(dir_path)

    lines = getattr(request.node, "_debug_log_buffer", [])
    fc = FrameCapture(node_camera, dir_path, lines, nodeid)
    # Stash so pytest_runtest_makereport can embed captures in HTML extras.
    request.node._ui_captures = fc.captures  # type: ignore[attr-defined]
    yield fc


# ---------- Session screen-on + per-node home reset ------------------------


@pytest.fixture(scope="session", autouse=True)
def _ui_screen_kept_on(
    ui_roles_present_session: list[str], hub_devices: dict[str, Any]
) -> Iterator[None]:
    """Keep every screen-bearing node's display on for the UI tier, and ensure
    its firmware streams the frame log.

    Why screen-on: `InputBroker::handleInputEvent` (src/input/InputBroker.cpp)
    silently DROPS any event that arrives while the screen is off — it just
    wakes the screen and returns. We set `display.screen_on_secs = 86400` per
    node at session start and restore the prior value at teardown.

    Why debug-log: the `Screen: frame …` lines the tier asserts on only reach us
    when `security.debug_log_api_enabled=True`; enable it per node (idempotent).
    """
    originals: dict[str, tuple[str, int | None]] = {}
    for role in ui_roles_present_session:
        port = _port_of(hub_devices[role])
        if not port:
            continue
        prior: int | None = None
        try:
            current = admin_mod.get_config(section="display", port=port)
            prior = int(current.get("config", {}).get("display", {}).get("screen_on_secs") or 0)
        except Exception:
            pass
        originals[role] = (port, prior)
        try:
            admin_mod.set_config("display.screen_on_secs", 86400, port=port)
        except Exception:
            pass
        try:
            admin_mod.set_debug_log_api(True, port=port)
        except Exception:
            pass
        # Wake the screen so the first test's first event isn't eaten.
        try:
            admin_mod.send_input_event(event_code=int(InputEventCode.FN_F1), port=port)
        except Exception:
            pass

    if originals:
        time.sleep(1.5)  # let the wake transitions finish

    try:
        yield
    finally:
        for _role, (port, prior) in originals.items():
            if prior is not None:
                try:
                    admin_mod.set_config("display.screen_on_secs", prior, port=port)
                except Exception:
                    pass


def _send_event(port: str, event: InputEventCode) -> None:
    try:
        admin_mod.send_input_event(event_code=int(event), port=port)
    except Exception:
        # Treat a failed event as soft — the subsequent frame-log assertion
        # surfaces the real problem with better context.
        pass


@pytest.fixture(autouse=True)
def ui_home_state(
    request: pytest.FixtureRequest,
    ui_role: str,
    ui_port: str,
    node_log_lines: list[str],
    _ui_screen_kept_on: None,
) -> Iterator[None]:
    """Before every UI test, jump the node under test to frame 0 (usually
    `home`) via FN_F1 and confirm it emitted the expected frame log.

    Why FN_F1 (not BACK): FN_F1 maps to `switchToFrame(0)` and ALWAYS produces a
    `reason=fn_f1` log line regardless of the frame the prior test left us on.
    BACK is context-sensitive and can silently fail to transition.

    Doubles as the per-node capability detector: if no `fn_f1` log arrives in
    5 s, the firmware wasn't baked with `USERPREFS_UI_TEST_LOG`, or this display
    (e.g. e-ink t_echo) doesn't drive the carousel frame log — skip this node's
    case with an actionable hint instead of a confusing assertion failure.
    """
    lines = node_log_lines
    start_len = len(lines)

    # First FN_F1 may be eaten by the screenWasOff guard; the second lands.
    _send_event(ui_port, InputEventCode.FN_F1)
    time.sleep(0.4)
    _send_event(ui_port, InputEventCode.FN_F1)

    try:
        wait_for_reason(lines, "fn_f1", timeout_s=5.0)
    except TimeoutError:
        _send_event(ui_port, InputEventCode.FN_F1)
        try:
            wait_for_reason(lines, "fn_f1", timeout_s=5.0)
        except TimeoutError:
            frame_lines = [ln for ln in lines[start_len:] if "Screen: frame" in ln]
            if frame_lines:
                pytest.skip(
                    f"ui_home_state[{ui_role}]: events fire but none reach Screen "
                    f"(saw {len(frame_lines)} frame line(s)). Device may be in an "
                    f"unusual state — try `--force-bake`."
                )
            else:
                pytest.skip(
                    f"ui_home_state[{ui_role}]: no `Screen: frame` log after FN_F1. "
                    f"Firmware not baked with USERPREFS_UI_TEST_LOG, this node is "
                    f"unresponsive, or this display doesn't drive the carousel "
                    f"frame log. Run with `--force-bake` to reflash."
                )
    yield


# ---------- Small helpers reused by tests ---------------------------------


def send_event(port: str, event: InputEventCode | int | str, **kwargs: Any) -> dict[str, Any]:
    """Thin wrapper so tests read `send_event(port, InputEventCode.RIGHT)`."""
    return admin_mod.send_input_event(event_code=event, port=port, **kwargs)


__all__ = [
    "UI_CAPABLE_ROLES",
    "FrameCapture",
    "FrameEvent",
    "camera_binding_for_role",
    "post_event_settle",
    "send_event",
    "wait_for_frame",
]
