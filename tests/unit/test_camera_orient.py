"""Unit tests for `camera.orient_frame` — the rotation/mirror transform the UI
tier applies so OCR sees an upright image regardless of how the bench camera is
physically mounted (the DB stores a per-camera rotation: cam 0/cam 2 are 180°).

Pure numpy/cv2 transform — no hardware. Skips if the [ui] extra isn't installed.
"""

from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from meshtastic_mcp.camera import orient_frame


def _marked():
    """A 4×6 BGR frame, black except a white top-left pixel — so we can track
    where the (0,0) corner lands after a transform."""
    f = np.zeros((4, 6, 3), dtype=np.uint8)
    f[0, 0] = (255, 255, 255)
    return f


def test_rotation_0_is_identity():
    out = orient_frame(_marked(), 0, False)
    assert out.shape == (4, 6, 3)
    assert tuple(out[0, 0]) == (255, 255, 255)


def test_rotation_180_moves_corner_to_opposite():
    out = orient_frame(_marked(), 180, False)
    assert out.shape == (4, 6, 3)
    assert tuple(out[3, 5]) == (255, 255, 255)
    assert tuple(out[0, 0]) == (0, 0, 0)


def test_rotation_90_cw_swaps_dims_and_corner():
    out = orient_frame(_marked(), 90, False)
    assert out.shape == (6, 4, 3)  # H/W swapped
    # (0,0) → (0, H-1) under a clockwise quarter turn.
    assert tuple(out[0, 3]) == (255, 255, 255)


def test_rotation_270_ccw_swaps_dims_and_corner():
    out = orient_frame(_marked(), 270, False)
    assert out.shape == (6, 4, 3)
    # (0,0) → (W-1, 0) under a counter-clockwise quarter turn.
    assert tuple(out[5, 0]) == (255, 255, 255)


def test_mirror_flips_horizontally():
    out = orient_frame(_marked(), 0, True)
    assert out.shape == (4, 6, 3)
    assert tuple(out[0, 5]) == (255, 255, 255)
    assert tuple(out[0, 0]) == (0, 0, 0)


def test_rotation_snaps_to_nearest_quarter():
    # 170° is closest to 180° → corner lands opposite.
    out = orient_frame(_marked(), 170, False)
    assert out.shape == (4, 6, 3)
    assert tuple(out[3, 5]) == (255, 255, 255)
