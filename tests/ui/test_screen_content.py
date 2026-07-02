"""Camera + OCR content assertions.

The other UI tests assert which *frame* the firmware logged; these assert that
the OLED actually **renders** what we expect, read back off the camera through
OCR. Matching is tolerant (normalize to lowercase alphanumerics, substring
containment) because OLED OCR is fuzzy. Skips cleanly when no OCR backend is
installed so the tier still runs on a capture-only machine.
"""

from __future__ import annotations

import time

import pytest

from meshtastic_mcp import info
from meshtastic_mcp import ocr as ocr_mod
from meshtastic_mcp.input_events import InputEventCode

from ._ocr_match import normalize, ocr_contains_any
from ._screen_log import get_current_frame
from .conftest import FrameCapture, post_event_settle, send_event


def _ocr_available() -> bool:
    try:
        return ocr_mod.backend_name() != "null"
    except Exception:
        return False


@pytest.mark.timeout(60)
def test_home_screen_renders_text(
    ui_port: str,
    frame_capture: FrameCapture,
) -> None:
    """With the screen kept on, the home frame shows readable content on camera
    — proves the OLED is lit and drawing, not blank or asleep."""
    if not _ocr_available():
        pytest.skip("no OCR backend (install the [ui] extra)")
    cap = frame_capture("home")
    ocr = cap.get("ocr_text") or ""
    assert normalize(ocr), f"home screen produced no OCR text — is the OLED on/visible? raw={ocr!r}"


@pytest.mark.timeout(90)
def test_home_screen_shows_device_identity(
    ui_port: str,
    frame_capture: FrameCapture,
) -> None:
    """The home/deviceFocused frame shows a token we can tie back to the device:
    its short or long name, region, or firmware version (digits OCR reliably)."""
    if not _ocr_available():
        pytest.skip("no OCR backend")
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

    cap = frame_capture("home-identity")
    ocr = cap.get("ocr_text") or ""
    assert ocr_contains_any(ocr, candidates, min_len=2), (
        f"home OCR {ocr!r} contained none of {candidates!r}"
    )


@pytest.mark.timeout(120)
def test_carousel_frames_render(
    ui_role: str,
    ui_port: str,
    frame_capture: FrameCapture,
    request: pytest.FixtureRequest,
) -> None:
    """Stepping the carousel forward with RIGHT, the screen stays lit and draws:
    a majority of frames yield readable OCR text on camera."""
    if not _ocr_available():
        pytest.skip("no OCR backend")
    lines: list[str] = request.node._debug_log_buffer
    start = get_current_frame(lines)
    assert start is not None, "no frame log — USERPREFS_UI_TEST_LOG not wired?"

    total = min(start.count, 6)
    rendered = 0
    settle = post_event_settle(ui_role)
    for i in range(total):
        cap = frame_capture(f"frame-{i}")
        if normalize(cap.get("ocr_text") or ""):
            rendered += 1
        send_event(ui_port, InputEventCode.RIGHT)
        time.sleep(settle)

    # OLED OCR is fuzzy — require a majority, not every frame (some are sparse
    # icon-only screens that OCR can't read).
    assert rendered >= (total + 1) // 2, (
        f"only {rendered}/{total} carousel frames produced OCR text — "
        "the screen may be off or the camera misaimed"
    )
