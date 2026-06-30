# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Meshtastic MCP server — device discovery, admin, recorder, and hardware-free e2e."""

from __future__ import annotations

try:
    # Written by hatch-vcs at build time (single source of truth: git tags).
    from ._version import __version__
except ImportError:  # pragma: no cover - editable installs without a build
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            __version__ = version("meshtastic-mcp")
        except PackageNotFoundError:
            __version__ = "0+unknown"
    except ImportError:
        __version__ = "0+unknown"

__all__ = ["__version__"]
