# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Bundled skills name only tools a default session lists."""

from __future__ import annotations

import re
from pathlib import Path

from meshtastic_mcp import toolsets

SKILLS = Path(__file__).resolve().parents[2] / "src" / "meshtastic_mcp" / "skills"


def test_skills_never_send_an_agent_to_a_legacy_tool() -> None:
    legacy = "|".join(sorted(toolsets._GROUPS["legacy"]))
    # A tool named in backticks or called; `admin.get_config(` is a Python function, not a tool.
    pattern = re.compile(rf"(?<![.\w])(?:`({legacy})`|({legacy})\()")
    hits = [
        f"{path.relative_to(SKILLS)}:{n}: {line.strip()}"
        for path in sorted(SKILLS.rglob("*.md"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == [], "\n".join(hits)
