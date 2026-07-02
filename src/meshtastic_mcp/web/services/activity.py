# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unified server-side activity stream for device actions.

A device action (flash, inject-nodedb, factory-reset, reboot) can run for
minutes while streaming little output, so a UI that only shows a button spinner
looks frozen. ``Activity`` wraps such an action and publishes ``action.update``
frames on the hub so the UI shows live phase + elapsed + last output line —
backed by the server, not just a client-side timer.

It mirrors :meth:`test_runner.TestRunner._heartbeat`: on enter it records a
monotonic start and publishes a ``started`` frame, then a 1 s background task
emits ``running`` frames even when the action is silent (so it never looks
wedged); on exit it cancels the heartbeat and publishes ``done`` (or ``error``
if the body raised). ``.line(s)`` / ``.phase(p)`` refresh the last output line
and coarse phase from the worker thread driving the blocking call.

Frame shape (topic ``action.update``)::

    {id, kind, target, phase, state, elapsed_s, last_line, ts}

``state`` is one of ``started`` | ``running`` | ``done`` | ``error``; ``id`` is
``f"{kind}:{target}:{monotonic_ns}"`` so concurrent actions on the same device
stay distinct.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("meshtastic_mcp.web.activity")

# How often the heartbeat emits a `running` frame for the in-flight action.
# Matches the client-side 1 s tick so server + client elapsed stay in step.
HEARTBEAT_S = 1.0


class Activity:
    """Async context manager that streams ``action.update`` frames for one
    device action. ``async with Activity(hub, kind, target) as act:`` wraps the
    blocking call; ``act.line``/``act.phase`` are safe to call from the worker
    thread running it (they hop back onto the loop via ``publish_threadsafe``)."""

    def __init__(self, hub, kind: str, target: str, phase: str | None = None) -> None:
        self.hub = hub
        self.kind = kind
        self.target = target
        # Stored as ``_phase`` so it doesn't shadow the ``phase()`` method; it
        # still rides the frame under the "phase" key.
        self._phase = phase
        self.last_line: str | None = None
        self.id = ""
        self._start = 0.0
        self._hb: asyncio.Task | None = None

    def _elapsed_s(self) -> float:
        return round(time.monotonic() - self._start, 1)

    def _frame(self, state: str) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "phase": self._phase,
            "state": state,
            "elapsed_s": self._elapsed_s(),
            "last_line": self.last_line,
            "ts": time.time(),
        }

    async def __aenter__(self) -> Activity:
        self._start = time.monotonic()
        self.id = f"{self.kind}:{self.target}:{time.monotonic_ns()}"
        await self.hub.publish("action.update", self._frame("started"))
        self._hb = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._hb is not None:
            self._hb.cancel()
            try:
                await self._hb
            except asyncio.CancelledError:
                pass
        await self.hub.publish(
            "action.update", self._frame("error" if exc_type is not None else "done")
        )
        return False  # never swallow the action's exception

    # --- worker-thread callbacks ------------------------------------------
    def line(self, s: str) -> None:
        """Record the newest output line and push a ``running`` frame. Safe from
        the worker thread driving the blocking action."""
        if s and s.strip():
            self.last_line = s
        self.hub.publish_threadsafe("action.update", self._frame("running"))

    def phase(self, p: str) -> None:
        """Advance the coarse phase (e.g. compiling → uploading) and push a
        ``running`` frame so the transition shows immediately."""
        self._phase = p
        self.hub.publish_threadsafe("action.update", self._frame("running"))

    # --- internals ---------------------------------------------------------
    async def _heartbeat(self) -> None:
        """Emit a periodic ``running`` frame so a silent multi-minute action
        still shows live elapsed (ticks between any ``.line``/``.phase`` calls)."""
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.hub.publish("action.update", self._frame("running"))
