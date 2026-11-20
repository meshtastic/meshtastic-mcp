# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""One long-lived SDK CLI per process, reached over its stdio `mcp` mode.

A one-shot `cli` run pays the JVM start and the radio handshake on every call. `cli mcp`
holds one radio session and answers tool calls on it, so a read after the first costs
milliseconds. A CLI built without `mcp` fails to start here, and the backend falls back to
one-shot runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from . import BackendError


class SdkSession:
    def __init__(self, cli: str) -> None:
        self._cli = cli
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client: Any = None
        self._lock = threading.Lock()
        self.target: str | None = None

    def _run(self, coro: Any, timeout_s: float) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout_s)

    async def _open(self) -> None:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        # The CLI needs this process's environment (JAVA_HOME, PATH); stdio clients pass none.
        transport = StdioTransport(command=self._cli, args=["mcp"], env=dict(os.environ))
        self._client = Client(transport, timeout=120)
        await self._client.__aenter__()

    def call(self, tool: str, args: dict[str, Any], timeout_s: float = 60.0) -> dict[str, Any]:
        with self._lock:
            if self._client is None:
                self._run(self._open(), 60.0)
            result = self._run(self._client.call_tool(tool, args, raise_on_error=False), timeout_s)
        text = result.content[0].text if result.content else ""
        if result.is_error:
            raise BackendError(f"the SDK CLI's {tool} failed: {text}")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise BackendError(f"the SDK CLI's {tool} returned {text[:120]!r}") from exc
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def ensure(self, spec: str, timeout_s: float) -> None:
        """Connect the session to `spec` unless it already holds it."""
        if self.target == spec:
            state = self.call("connection_state", {}, timeout_s)
            if state.get("state") == "Connected":
                return
        self.call(
            "connect", {"transport": spec, "timeout_ms": int(timeout_s * 1000)}, timeout_s + 30
        )
        self.target = spec


_session: SdkSession | None = None
_unavailable: str | None = None
_guard = threading.Lock()


def session(cli: str) -> SdkSession | None:
    """The process's session, started on first use; None when the CLI has no `mcp` mode."""
    global _session, _unavailable
    with _guard:
        if _session is not None:
            return _session
        if _unavailable is not None:
            return None
        candidate = SdkSession(cli)
        try:
            candidate.call("connection_state", {}, 60.0)
        except Exception as exc:  # no `mcp` mode, or the JVM would not start
            _unavailable = str(exc)
            return None
        _session = candidate
        return _session
