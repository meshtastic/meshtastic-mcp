#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Validate bundled SKILL.md files against the Agent Skills spec.

Rules (https://agentskills.io/specification):
  - YAML frontmatter present and terminated (`---` … `---`)
  - name: required; `^[a-z0-9]+(-[a-z0-9]+)*$`; <=64 chars; equals its folder name
  - description: required; <=1024 chars
  - no '<' or '>' anywhere in frontmatter (prompt-injection guard)

No external deps: only `name`/`description` are inspected and both are single-line
scalars in our skills.
# ponytail: line-based frontmatter parse, not full YAML. If a skill ever uses a
# block scalar (`description: |`) or quoted multiline, switch to PyYAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "meshtastic_mcp" / "skills"


def frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return text[text.find("\n", 3) + 1 : end] if end != -1 else None


def scalar(fm: str, key: str) -> str | None:
    m = re.search(rf"(?m)^{key}:[ \t]*(.*)$", fm)
    return m.group(1).strip() if m else None


def check(skill: Path) -> list[str]:
    fm = frontmatter(skill.read_text())
    if fm is None:
        return ["missing or unterminated YAML frontmatter"]
    errs: list[str] = []
    if "<" in fm or ">" in fm:
        errs.append("frontmatter contains '<' or '>' (prompt-injection risk)")
    name = scalar(fm, "name")
    if not name:
        errs.append("missing 'name'")
    else:
        if len(name) > 64:
            errs.append(f"name is {len(name)} chars (>64)")
        if not NAME_RE.match(name):
            errs.append(f"name '{name}' must match {NAME_RE.pattern}")
        if name != skill.parent.name:
            errs.append(f"name '{name}' != folder '{skill.parent.name}'")
    desc = scalar(fm, "description")
    if not desc:
        errs.append("missing 'description'")
    elif len(desc) > 1024:
        errs.append(f"description is {len(desc)} chars (>1024)")
    return errs


def _self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "Bad_Name" / "SKILL.md"
        bad.parent.mkdir()
        bad.write_text("---\nname: Bad_Name\ndescription: has <angle> brackets\n---\nbody\n")
        errs = check(bad)
        assert any("must match" in e for e in errs), errs  # bad name chars
        assert any("'<' or '>'" in e for e in errs), errs  # angle brackets
        good = Path(d) / "good-skill" / "SKILL.md"
        good.parent.mkdir()
        good.write_text("---\nname: good-skill\ndescription: fine\n---\nbody\n")
        assert check(good) == [], check(good)
    print("self-test ok")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    skills = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    if not skills:
        print(f"no SKILL.md found under {SKILLS_DIR}", file=sys.stderr)
        return 1
    failed = False
    for s in skills:
        errs = check(s)
        rel = s.relative_to(SKILLS_DIR.parent)
        if errs:
            failed = True
            print(f"FAIL {rel}")
            print("\n".join(f"  - {e}" for e in errs))
        else:
            print(f"ok   {rel}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
