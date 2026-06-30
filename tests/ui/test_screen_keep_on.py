"""Screen-on provisioning is what makes input events land.

InputBroker drops any event that arrives while the screen is off (it just wakes
the screen and returns — src/input/InputBroker.cpp). FleetSuite's keep-alive
provisions ``display.screen_on_secs`` high so the OLED never sleeps and the
periodic pokes actually register. This test verifies both halves: the config is
provisioned to keep the screen on, and an input event then produces a frame
transition instead of being swallowed.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp import admin
from meshtastic_mcp.input_events import InputEventCode

from ._screen_log import wait_for_any_frame
from .conftest import FrameCapture, send_event


@pytest.mark.timeout(90)
def test_screen_on_provisioned(ui_port: str) -> None:
    """The session keeps the OLED on (display.screen_on_secs high) for the UI
    tier — confirm the device actually reports it provisioned."""
    cfg = admin.get_config(section="display", port=ui_port)["config"]["display"]
    secs = int(cfg.get("screen_on_secs") or 0)
    assert secs >= 3600, (
        f"screen_on_secs={secs}: OLED not provisioned to stay on — keep-alive "
        "provisioning (or the UI session fixture) didn't apply"
    )


@pytest.mark.timeout(90)
def test_input_lands_while_screen_kept_on(
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """With the screen kept on, an input event registers (a frame transition
    fires) rather than being dropped by InputBroker's screen-off guard."""
    lines: list[str] = request.node._debug_log_buffer
    frame_capture("before-input")
    send_event(ui_port, InputEventCode.RIGHT)
    evt = wait_for_any_frame(lines, timeout_s=5.0)
    frame_capture("after-input")
    assert evt is not None, "input event was dropped — screen likely off"
