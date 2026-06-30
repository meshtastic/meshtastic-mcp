# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Replay engine — serve a capture as a simulated Meshtastic TCP device.

The inverse of the recorder: where `recorder/` subscribes to a live mesh and
writes packets out, `replay/` reads a capture (SQLite DB, recorder JSONL, or a
generated synthetic mesh) and streams it to a connecting app over the standard
Meshtastic stream protocol, restamped to "now".

  - `capture` — source loaders normalised to a single `Capture` shape.
  - `engine`  — the TCP device + want-config handshake + paced stream loop,
                driven by a process-global `ReplayManager` (start/status/stop).
  - `sim`     — synthetic mesh generator (MeshCon), statistics-driven and seeded.
"""

from __future__ import annotations

from . import build, capture, fuzz, sim
from .capture import (
    Capture,
    ChannelSpec,
    NodeRow,
    channel_hash,
    from_events,
    from_recorder_jsonl,
    from_sqlite,
)
from .engine import PortInUseError, ReplayManager, ReplayParams, ReplaySession, get_manager
from .fuzz import PRESET_NAMES as FUZZ_PRESETS
from .fuzz import FuzzConfig, Fuzzer

__all__ = [
    "FUZZ_PRESETS",
    "Capture",
    "ChannelSpec",
    "FuzzConfig",
    "Fuzzer",
    "NodeRow",
    "PortInUseError",
    "ReplayManager",
    "ReplayParams",
    "ReplaySession",
    "build",
    "capture",
    "channel_hash",
    "from_events",
    "from_recorder_jsonl",
    "from_sqlite",
    "fuzz",
    "get_manager",
    "sim",
]
