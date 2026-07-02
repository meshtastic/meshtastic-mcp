# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end click-through screen tests, asserted on each node's own camera.

These walk a node's on-device UI with button events and assert on what the
camera FleetSuite has bound to that node actually shows (via OCR). The whole
module is parametrized per screen-bearing role (`ui_role`) by the tier's
`pytest_generate_tests`, so each test runs once per node (esp32s3 / t_echo /
heltec_t114), each using its own port and its own (rotation-corrected) camera.

Captures land in `tests/ui_captures/<seed>/<nodeid>/` with a transcript. Nodes
whose firmware doesn't drive the carousel frame log (or that are unresponsive)
skip cleanly via the `ui_home_state` guard.
"""

from __future__ import annotations

import time

import pytest

from meshtastic_mcp import info
from meshtastic_mcp import ocr as ocr_mod
from meshtastic_mcp.input_events import InputEventCode

from ._ocr_match import normalize, ocr_contains_any
from ._screen_log import get_current_frame, wait_for_reason
from .conftest import FrameCapture, post_event_settle, send_event


def _ocr_available() -> bool:
    try:
        return ocr_mod.backend_name() != "null"
    except Exception:
        return False


@pytest.mark.timeout(240)
def test_clickthrough_carousel_walk_renders_on_node_camera(
    ui_role: str,
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """Click RIGHT all the way around the carousel: every press advances the
    frame, we visit every distinct screen, and a majority render readable
    content on THIS node's camera."""
    if not _ocr_available():
        pytest.skip("no OCR backend (install the [ui] extra)")
    lines: list[str] = request.node._debug_log_buffer
    start = get_current_frame(lines)
    assert start is not None, "no frame log — USERPREFS_UI_TEST_LOG not wired?"

    settle = post_event_settle(ui_role)
    count = start.count
    seen_idx = {start.idx}

    time.sleep(settle)
    first = frame_capture("frame-0")
    rendered = 1 if normalize(first.get("ocr_text") or "") else 0

    # count-1 RIGHTs walk the whole ring back to where we started.
    for step in range(1, count):
        send_event(ui_port, InputEventCode.RIGHT)
        try:
            evt = wait_for_reason(lines, "next", timeout_s=5.0)
        except TimeoutError:
            pytest.fail(f"RIGHT #{step} produced no frame transition on {ui_role!r}")
        seen_idx.add(evt.idx)
        time.sleep(settle)  # let the (slow e-ink) draw land before the grab
        cap = frame_capture(f"frame-{step}")
        if normalize(cap.get("ocr_text") or ""):
            rendered += 1

    assert len(seen_idx) == count, (
        f"{ui_role!r}: expected {count} distinct frames over the walk, saw {sorted(seen_idx)}"
    )
    # OCR is fuzzy and some frames are icon-only — require a majority readable.
    assert rendered >= (count + 1) // 2, (
        f"{ui_role!r}: only {rendered}/{count} frames produced OCR text on its "
        "camera — screen may be off, asleep, or the camera misaimed"
    )


@pytest.mark.timeout(150)
def test_clickthrough_menu_overlay_changes_node_camera(
    ui_role: str,
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """SELECT on the home frame opens the menu overlay (drawn on top, no frame
    transition); the node's camera OCR changes. BACK dismisses it."""
    if not _ocr_available():
        pytest.skip("no OCR backend")
    lines: list[str] = request.node._debug_log_buffer
    start = get_current_frame(lines)
    assert start is not None
    if start.name not in ("home", "deviceFocused"):
        pytest.skip(f"SELECT on {start.name!r} doesn't open the home menu")

    settle = max(post_event_settle(ui_role), 0.8)
    time.sleep(settle)
    before = (frame_capture("before-select").get("ocr_text") or "").strip()

    send_event(ui_port, InputEventCode.SELECT)
    time.sleep(settle)
    opened = (frame_capture("after-select").get("ocr_text") or "").strip()

    # Overlay is drawn on top (no frame log), so assert via the camera image.
    if before and opened:
        assert normalize(before) != normalize(opened), (
            f"{ui_role!r}: SELECT produced no visible change on camera; both read {before!r}"
        )

    send_event(ui_port, InputEventCode.BACK)
    time.sleep(settle)
    frame_capture("after-back")


@pytest.mark.timeout(150)
def test_clickthrough_home_identity_after_walk(
    ui_role: str,
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """Wander a couple frames, FN_F1 back to frame 0, and confirm the node's
    camera shows an identity token (short/long name, region, or fw) — tying the
    captured image back to the device's actual provisioned state."""
    if not _ocr_available():
        pytest.skip("no OCR backend")
    lines: list[str] = request.node._debug_log_buffer
    settle = post_event_settle(ui_role)

    for _ in range(2):
        send_event(ui_port, InputEventCode.RIGHT)
        time.sleep(0.3)

    send_event(ui_port, InputEventCode.FN_F1)
    try:
        wait_for_reason(lines, "fn_f1", timeout_s=5.0)
    except TimeoutError:
        pytest.skip(f"{ui_role!r}: FN_F1 didn't return to frame 0")
    time.sleep(settle)

    di = info.device_info(port=ui_port, timeout_s=8.0)
    candidates: list[str | None] = [
        di.get("short_name"),
        di.get("long_name"),
        di.get("region"),
    ]
    fw = di.get("firmware_version") or ""
    if fw:
        parts = fw.split(".")
        candidates.append(".".join(parts[:2]))  # "2.8"
        candidates.append(parts[0])  # "2"
    candidates = [c for c in candidates if c and len(normalize(c)) >= 2]
    if not candidates:
        pytest.skip("device exposes no identity tokens to match")

    ocr = frame_capture("home-after-walk").get("ocr_text") or ""
    assert ocr_contains_any(ocr, candidates, min_len=2), (
        f"{ui_role!r}: home OCR {ocr!r} matched none of {candidates!r}"
    )
