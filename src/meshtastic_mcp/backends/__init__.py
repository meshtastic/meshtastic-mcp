# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Device backends: what the device tools run on a radio.

A backend returns plain dicts, never a meshtastic-python interface or an SDK envelope, so a tool
works the same over either. `python` is meshtastic-python in this process; `sdk` is the Kotlin
SDK's CLI. `MESHTASTIC_MCP_BACKEND` picks one; a `ble:` target needs `sdk`.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from typing import Any, Protocol

BACKEND_ENV = "MESHTASTIC_MCP_BACKEND"

# The parts a read can ask for. Config sections are read through `config`. `url` carries the
# channel keys, so only a read that names it gets it.
PARTS = ("summary", "config", "channels", "nodes", "network", "url")
UNSECRET_PARTS = tuple(p for p in PARTS if p != "url")


class BackendError(RuntimeError):
    """A backend could not serve a request."""


class Backend(Protocol):
    name: str
    parts: frozenset[str]

    def read(
        self,
        target: str | None,
        parts: Collection[str],
        sections: Collection[str] | None,
        detail: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Read `parts` over one connection.

        Returns a dict keyed by part, plus `target`, and `errors` for parts that failed while
        others succeeded. `sections` limits `config`; None means every section.
        """
        ...


def for_target(target: str | None) -> Backend:
    """The backend that serves `target`."""
    wanted = os.environ.get(BACKEND_ENV, "").strip().lower()
    if wanted not in {"", "python", "sdk"}:
        raise BackendError(f"{BACKEND_ENV}={wanted!r}; choose python or sdk")
    if (target or "").startswith("ble:") or wanted == "sdk":
        from .sdk import SdkBackend

        return SdkBackend()
    from .python import PythonBackend

    return PythonBackend()
