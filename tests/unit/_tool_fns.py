# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Find a tool's function in the `tools/` module that defines it."""

from __future__ import annotations

from typing import Any

from meshtastic_mcp.tools import (
    android,
    app_drive,
    apple,
    core,
    firmware,
    hardware,
    replay,
    rf,
    sdk,
)

_MODULES = (core, hardware, firmware, replay, rf, android, apple, app_drive, sdk)


def tool_fn(name: str) -> Any:
    for mod in _MODULES:
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"no tool module defines {name!r}")
