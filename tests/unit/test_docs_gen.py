# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""The tool reference is generated from the registered tools and kept current."""

from __future__ import annotations

import re
from pathlib import Path

from meshtastic_mcp import docs_gen

from .test_toolsets import _defined_tools

ROOT = Path(__file__).resolve().parents[2]


def test_the_committed_reference_matches_the_tools() -> None:
    assert docs_gen.generate(ROOT, check=True) == 0, "run `meshtastic-mcp docs generate`"


def test_every_defined_tool_has_a_reference_entry() -> None:
    documented = set(re.findall(r"^### `([a-z0-9_]+)`", (ROOT / "docs/tools.md").read_text(), re.M))
    assert _defined_tools() - documented == set()


def test_parameters_render_with_their_types_and_defaults() -> None:
    tool = {
        "name": "inspect",
        "toolset": "core",
        "description": "Read a radio.",
        "hints": ["read-only"],
        "parameters": {
            "properties": {
                "node": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "detail": {"enum": ["brief", "full"], "default": "brief"},
            },
            "required": [],
        },
    }
    text = docs_gen.render_reference([tool])
    assert "### `inspect`" in text and "Read-only." in text
    assert "| `node` | string | `null` |" in text
    assert r'| `detail` | `brief` \| `full` | `"brief"` |' in text
