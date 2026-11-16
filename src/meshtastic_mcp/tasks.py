# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Optional MCP background-task support (the `[tasks]` extra).

A tool marked `task=True` runs as a background task for clients that ask for one, and runs inline
for everyone else, so the poll-based tools stay the path for clients without task support.
"""

from __future__ import annotations

from typing import Any

try:
    from fastmcp_tasks import TasksExtension

    _extension: type[TasksExtension] | None = TasksExtension
except ImportError:  # the [tasks] extra is not installed
    _extension = None


def available() -> bool:
    return _extension is not None


def install(app: Any) -> None:
    if _extension is not None:
        app.add_extension(_extension())


def task_kwargs() -> dict[str, Any]:
    """Decorator kwargs that opt a tool into background tasks when the extension is present."""
    return {"task": True} if available() else {}
