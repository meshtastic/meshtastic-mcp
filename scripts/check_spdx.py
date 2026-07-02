#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CI gate: every source file must carry the repo's SPDX header.

Checks the first 400 bytes of each matched file for `SPDX-License-Identifier`
(the copyright line rides with it by convention). Exits non-zero listing any
offenders, so new files can't ship unstamped.

Run from the repo root: `python scripts/check_spdx.py`
"""

from __future__ import annotations

import pathlib
import sys

PATTERNS = [
    "src/**/*.py",
    "tests/**/*.py",
    "scripts/*.py",
    "scripts/*.sh",
    "web-ui/src/**/*.py",
    "web-ui/src/**/*.ts",
    "web-ui/src/**/*.vue",
    "web-ui/env.d.ts",
    "run-tests.sh",
]

# Generated or vendored files that legitimately have no header.
SKIP_NAMES = {"_version.py"}
SKIP_PARTS = {".venv", "node_modules", "__pycache__", "static"}


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    missing: list[str] = []
    for pattern in PATTERNS:
        for path in sorted(root.glob(pattern)):
            if path.name in SKIP_NAMES or SKIP_PARTS.intersection(path.parts):
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
            if "SPDX-License-Identifier" not in head:
                missing.append(str(path.relative_to(root)))
    if missing:
        print("missing SPDX header (SPDX-License-Identifier within the first 400 bytes):")
        for m in missing:
            print(f"  {m}")
        return 1
    print("SPDX headers: all source files stamped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
