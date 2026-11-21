# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""One server-log line per tool call, so an operator can see why a call failed.

Lines go to stderr (journald under `serve`), never stdout, which carries the stdio protocol.
Only argument names are logged; the one value logged is the target radio, a port or name.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

log = logging.getLogger("meshtastic_mcp.calls")

_TARGET_ARGS = ("node", "port", "device", "target", "serial")
# Polled reads that would bury the failures at INFO.
_QUIET = frozenset(
    {
        "build_poll",
        "flash_poll",
        "events_window",
        "logs_window",
        "packets_window",
        "recorder_status",
        "sim_status",
        "replay_status",
        "wait_for",
    }
)


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stderr` is at emit time."""

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def _ensure_handler() -> None:
    if any(isinstance(h, _StderrHandler) for h in log.handlers):
        return
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _target(args: dict[str, Any]) -> str | None:
    for key in _TARGET_ARGS:
        value = args.get(key)
        if isinstance(value, (str, int)) and value != "":
            return str(value)
    return None


def _client(context: Any) -> str | None:
    ctx = getattr(context, "fastmcp_context", None)
    if ctx is None:
        return None
    for attr in ("client_id", "session_id"):
        try:
            value = getattr(ctx, attr)
        except Exception:  # no session in this transport
            continue
        if value:
            return str(value)[:12]
    return None


def _job_id(result: Any) -> str | None:
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict):
        job = data.get("job_id") or data.get("build_id")
        if isinstance(job, str):
            return job
    return None


def _prefix(name: str, args: dict[str, Any], client: str | None) -> str:
    parts = [f"tool={name}"]
    target = _target(args)
    if target is not None:
        parts.append(f"target={target}")
    if client is not None:
        parts.append(f"client={client}")
    return " ".join(parts)


class CallLogMiddleware(Middleware):
    def __init__(self) -> None:
        _ensure_handler()

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = getattr(context.message, "name", "?")
        args = dict(getattr(context.message, "arguments", None) or {})
        head = _prefix(name, args, _client(context))
        start = time.monotonic()
        try:
            result = await call_next(context)
        except ToolError as exc:
            cause = exc.__cause__
            if cause is not None and not isinstance(cause, ToolError):
                # fastmcp wraps an unexpected exception in a ToolError; log the original.
                log.warning(
                    "%s raised %s in %.2fs args=[%s]: %s",
                    head,
                    type(cause).__name__,
                    time.monotonic() - start,
                    ",".join(sorted(args)),
                    cause,
                    exc_info=cause,
                )
            else:
                log.warning(
                    "%s failed in %.2fs args=[%s]: %s",
                    head,
                    time.monotonic() - start,
                    ",".join(sorted(args)),
                    exc,
                )
            raise
        except Exception as exc:
            log.warning(
                "%s raised %s in %.2fs args=[%s]: %s",
                head,
                type(exc).__name__,
                time.monotonic() - start,
                ",".join(sorted(args)),
                exc,
                exc_info=True,
            )
            raise
        elapsed = time.monotonic() - start
        job = _job_id(result)
        level = logging.DEBUG if name in _QUIET else logging.INFO
        log.log(level, "%s ok in %.2fs%s", head, elapsed, f" job={job}" if job else "")
        return result
