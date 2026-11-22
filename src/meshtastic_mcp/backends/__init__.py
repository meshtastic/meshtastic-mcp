# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Device backends: what the device tools run on a radio.

A backend returns plain dicts, never a meshtastic-python interface or an SDK envelope, so a tool
works the same over either. `python` is meshtastic-python in this process; `sdk` is the Kotlin
SDK's CLI. `MESHTASTIC_MCP_BACKEND` picks one (default `python`); both reach `ble:` targets.
"""

from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable, Collection, Iterator
from typing import Any, Protocol

BACKEND_ENV = "MESHTASTIC_MCP_BACKEND"

# The parts a read can ask for. Config sections are read through `config`. `url` carries the
# channel keys, so only a read that names it gets it.
PARTS = ("summary", "config", "channels", "nodes", "network", "url", "contact_url")
UNSECRET_PARTS = tuple(p for p in PARTS if p not in ("url", "contact_url"))


class BackendError(RuntimeError):
    """A backend could not serve a request."""


@contextlib.contextmanager
def library_guard() -> Iterator[None]:
    """Turn meshtastic-python's refusals into a BackendError.

    The library prints a refusal to stdout and calls `sys.exit`; on stdio, stdout is the MCP
    stream and the exit would end the server.
    """
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            yield
    except SystemExit as exc:
        said = printed.getvalue().strip() or f"exit {exc.code}"
        raise BackendError(f"meshtastic-python refused the request: {said}") from None


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

    def configure(
        self,
        target: str | None,
        changes: dict[str, Any] | None,
        restore: dict[str, Any] | None,
        dry_run: bool,
        timeout_s: float,
        save_snapshot: Callable[[str, dict[str, Any]], dict[str, Any]],
        dest: str | int | None = None,
    ) -> dict[str, Any]:
        """Apply `changes` (dot-paths) and/or `restore` (a snapshot's config), then read back.

        `save_snapshot(port, config)` stores the config as it was before any write and returns
        what the result reports as `snapshot`. `dest` writes to another node over PKI admin.
        """
        ...

    def admin(
        self,
        target: str | None,
        action: str,
        node_id: str | int | None,
        seconds: int,
        wait_back: bool,
        timeout_s: float,
        dest: str | int | None = None,
        value: Any = None,
    ) -> dict[str, Any]:
        """Run a radio-level `action`; `wait_back` waits for a restarted radio to answer.

        `dest` sends it to another node over PKI admin; `value` is the action's argument
        (`set_time` seconds, `add_contact` URL).
        """
        ...

    def import_channels(
        self, target: str | None, url: str, add_only: bool, timeout_s: float
    ) -> dict[str, Any]:
        """Import a channel URL: replace the channel set, or with `add_only` add new ones."""
        ...

    def input_event(
        self, target: str | None, event_code: int, kb_char: int, touch_x: int, touch_y: int
    ) -> dict[str, Any]:
        """Inject an input event (button, key, touch) into the radio's UI."""

    def send(
        self,
        target: str | None,
        to: str | int | None,
        payload: dict[str, Any],
        channel: int,
        want_ack: bool,
        hop_limit: int | None,
        wait_s: float,
    ) -> dict[str, Any]:
        """Send one of `text`, `position`, `waypoint` or `request` (`payload` has one key)."""
        ...


def for_target(target: str | None) -> Backend:
    """The backend that serves `target`."""
    wanted = os.environ.get(BACKEND_ENV, "").strip().lower()
    if wanted not in {"", "python", "sdk"}:
        raise BackendError(f"{BACKEND_ENV}={wanted!r}; choose python or sdk")
    if wanted == "sdk":
        from .sdk import SdkBackend

        return SdkBackend()
    from .python import PythonBackend

    return PythonBackend()
