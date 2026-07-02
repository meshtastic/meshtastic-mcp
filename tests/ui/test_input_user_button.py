# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""USER_PRESS advances the screen carousel.

This is the exact event FleetSuite's screen keep-alive injects every cycle to
keep the OLED awake and cycling for the cameras — the short press of the
physical user button, delivered as an InputBroker event over an admin message.
This test proves that press advances the carousel on the bench's UI board (so
the keep-alive actually rotates frames, not just wakes the screen).
"""

from __future__ import annotations

import pytest

from meshtastic_mcp.input_events import InputEventCode

from ._screen_log import get_current_frame, wait_for_any_frame
from .conftest import FrameCapture, send_event


@pytest.mark.timeout(60)
def test_user_press_advances_carousel(
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    lines: list[str] = request.node._debug_log_buffer
    start = get_current_frame(lines)
    assert start is not None, "no frame log — USERPREFS_UI_TEST_LOG not wired?"
    if start.count <= 1:
        pytest.skip("single-frame carousel — nothing to advance")

    frame_capture("before-user-press")
    send_event(ui_port, InputEventCode.USER_PRESS)

    # A press must move the carousel — InputBroker logs a frame transition. If
    # USER_PRESS is unmapped on this firmware, this times out with the captured
    # frame history, which is itself the useful signal (switch the keep-alive
    # event in that case).
    evt = wait_for_any_frame(lines, timeout_s=5.0)
    frame_capture("after-user-press")
    assert evt.idx != start.idx, (
        f"USER_PRESS did not move off frame {start.idx} (landed on {evt.idx})"
    )


@pytest.mark.timeout(90)
def test_repeated_user_press_walks_the_carousel(
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """Several user-button presses visit several distinct frames — the keep-alive
    cycles through the UI rather than toggling between two."""
    lines: list[str] = request.node._debug_log_buffer
    start = get_current_frame(lines)
    assert start is not None
    if start.count <= 2:
        pytest.skip("carousel too short to walk")

    visited = {start.idx}
    frame_capture("start")
    for step in range(min(start.count, 5)):
        send_event(ui_port, InputEventCode.USER_PRESS)
        evt = wait_for_any_frame(lines, timeout_s=5.0)
        visited.add(evt.idx)
        frame_capture(f"press-{step + 1}")

    assert len(visited) >= 3, f"user presses only reached frames {sorted(visited)}"
