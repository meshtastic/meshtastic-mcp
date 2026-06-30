"""Tolerant matching for OLED OCR.

The camera reads a tiny, low-DPI monochrome screen, so exact text rarely
survives OCR — spacing, case, and punctuation are unreliable. Normalize to
lowercase alphanumerics and test containment of distinctive tokens.
"""

from __future__ import annotations

import re


def normalize(text: str | None) -> str:
    """Lowercase, keep only [a-z0-9] — collapses spacing/case/punctuation noise."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def ocr_contains(ocr: str | None, candidate: str | None, *, min_len: int = 3) -> bool:
    c = normalize(candidate)
    return len(c) >= min_len and c in normalize(ocr)


def ocr_contains_any(ocr: str | None, candidates: list[str | None], *, min_len: int = 3) -> bool:
    return any(ocr_contains(ocr, c, min_len=min_len) for c in candidates if c)
