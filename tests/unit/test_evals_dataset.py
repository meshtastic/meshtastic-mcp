# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Keep the tool-selection eval honest: every expected_tool must be a registered MCP tool.

Without this, renaming/removing a tool silently rots the eval dataset (it would keep scoring
agents against a tool that no longer exists).
"""

from __future__ import annotations

import csv
import pathlib

import pytest

_DATASET = pathlib.Path(__file__).resolve().parents[2] / ".github" / "evals" / "tool-selection.csv"


@pytest.fixture
def known_tools():
    from meshtastic_mcp import server  # deferred (recorder/pubsub topic ownership)

    # Union of registered tools + all capability-gated tools so the guard is
    # valid regardless of this host's active capabilities.
    return (
        set(server.app._tool_manager._tools)
        | set(server._FIRMWARE_TOOLS)
        | set(server._ANDROID_TOOLS)
    )


def test_dataset_exists_and_has_rows() -> None:
    assert _DATASET.is_file()
    rows = list(csv.DictReader(_DATASET.read_text().splitlines()))
    assert len(rows) >= 10
    assert all(r["intent"] and r["expected_tool"] for r in rows)


def test_every_expected_tool_is_a_known_tool(known_tools) -> None:
    rows = list(csv.DictReader(_DATASET.read_text().splitlines()))
    unknown = sorted({r["expected_tool"] for r in rows} - known_tools)
    assert not unknown, f"tool-selection.csv references unknown tools: {unknown}"
