# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`env_vars.ENV_VARS` names every environment variable the package reads, and no other."""

from __future__ import annotations

import re
from pathlib import Path

from meshtastic_mcp import env_vars

SRC = Path(__file__).resolve().parents[2] / "src" / "meshtastic_mcp"
NAME = re.compile(r"""["']((?:MESHTASTIC|DISCORD)_[A-Z0-9_]+|ANDROID_HOME|ANDROID_SDK_ROOT)["']""")


def _read_in_code() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "env_vars.py":
            continue
        found.update(NAME.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_variable_the_code_reads_is_documented() -> None:
    documented = {v.name for v in env_vars.ENV_VARS}
    missing = _read_in_code() - documented
    assert not missing, f"add these to env_vars.ENV_VARS: {sorted(missing)}"


def test_every_documented_variable_is_still_read() -> None:
    stale = {v.name for v in env_vars.ENV_VARS} - _read_in_code()
    assert not stale, f"no code reads these any more: {sorted(stale)}"


def test_each_variable_names_a_known_section_once() -> None:
    names = [v.name for v in env_vars.ENV_VARS]
    assert len(names) == len(set(names))
    assert {v.section for v in env_vars.ENV_VARS} <= set(env_vars.SECTIONS)
