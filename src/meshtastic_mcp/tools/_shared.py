# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Helpers that more than one tool module uses."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
from fastmcp import Context
from fastmcp.exceptions import ToolError

from .. import confirm as confirm_mod
from .. import hw_tools, jobs

# Tool helpers log as the server.
log = logging.getLogger("meshtastic_mcp.server")


# ---------------------------------------------------------------------------
# Output format helpers
# ---------------------------------------------------------------------------


def _to_markdown_table(rows: list[dict[str, Any]]) -> str:
    """Render a list of flat dicts as a GitHub-flavoured Markdown table.

    Saves 40–60% tokens vs JSON on large result sets by emitting field names
    once in the header row instead of on every row.
    """
    if not rows:
        return "(no results)"
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


async def _confirmed_hw_call(
    ctx: Context, call: Callable[[bool], dict[str, Any]], confirm: bool
) -> dict[str, Any]:
    """Run a `hw_tools` call, asking the human only when it turns out to be destructive.

    `hw_tools` refuses a destructive call before any side effect, so a probe with `confirm=False`
    is safe: a harmless subcommand just runs, and a destructive one raises `ConfirmRequired`. In
    `human` policy the probe always uses `confirm=False`, so every destructive call asks.
    """
    human = confirm_mod.policy() == confirm_mod.HUMAN
    try:
        result = await _off_loop(lambda: call(False if human else confirm))
    except hw_tools.ConfirmRequired as exc:
        pending = await confirm_mod.gate(ctx, f"run {exc.what}", confirm, irreversible=True)
        if pending is not None:
            return cast(dict[str, Any], pending)
        result = await _off_loop(lambda: call(True))
    return _raise_if_failed(result, "the command")


async def _off_loop(fn: Callable[[], Any]) -> Any:
    """Run blocking device work on a worker thread, as FastMCP does for sync tools.

    A gated tool is `async` so it can ask the human, but the serial, subprocess and sleep work
    after the gate would otherwise hold the event loop and stall every other request. What the
    work reports through `jobs` (an esptool, nrfutil or picotool run) goes to the calling
    client as progress.
    """
    from fastmcp.server.dependencies import get_context

    try:
        ctx = get_context()
    except RuntimeError:  # not inside a tool call
        return await anyio.to_thread.run_sync(fn)
    progress = jobs.Progress()

    def _tracked() -> Any:
        with jobs.tracking(progress):
            return fn()

    forwarder = jobs.forward_progress(progress, ctx.report_progress)
    try:
        return await anyio.to_thread.run_sync(_tracked)
    finally:
        forwarder.cancel()


def _output_tail(value: Any, limit: int = 1500) -> str:
    text = "\n".join(value) if isinstance(value, list) else str(value or "")
    return text.strip()[-limit:]


def _raise_if_failed(result: dict[str, Any], what: str) -> dict[str, Any]:
    """Turn a nonzero exit (or a flash that went to the wrong port) into a tool error.

    The flash and hardware helpers report failure as `exit_code != 0` in an ordinary result, which a
    client reads as success; the tail of the output goes into the error so the cause is visible.
    """
    code = result.get("exit_code")
    mismatch = result.get("upload_port_mismatch")
    if code in (0, None) and not mismatch:
        return result
    # Tools put the cause in either stream (adafruit-nrfutil prints it to stdout), so show both.
    tail = "\n".join(
        part
        for part in (
            _output_tail(result.get("stdout_tail") or result.get("log_tail"), 1000),
            _output_tail(result.get("stderr_tail"), 500),
        )
        if part
    )
    detail = f": {mismatch}" if mismatch else ""
    raise ToolError(f"{what} failed with exit code {code}{detail}\n{tail}".rstrip())


async def _run_job(
    ctx: Context,
    kind: str,
    label: str,
    fn: Callable[[], dict[str, Any]],
    poll_tool: str | None = "flash_poll",
) -> dict[str, Any]:
    """Run a blocking flash function as a tracked job, surfacing its failure as a tool error.

    The job id goes to the client as a log message before the wait: a flash cannot be canceled,
    so a caller whose task is canceled needs the id to follow the write with `poll_tool`. The
    job's progress goes to the client while it waits.
    """

    async def _announce(job_id: str) -> None:
        follow = f", and {poll_tool}(job_id='{job_id}') follows it" if poll_tool else ""
        try:
            await ctx.info(
                f"{kind} job {job_id} started for {label}; it keeps running if this call is "
                f"canceled{follow}"
            )
        except Exception as exc:  # a client or task with no log channel still gets the result
            log.debug("could not announce job %s: %s", job_id, exc)

    try:
        result = await jobs.run_sync(
            kind, label, fn, on_started=_announce, on_progress=ctx.report_progress
        )
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc
    return _raise_if_failed(result, f"flash of {label}")


def _capture_dir(out_dir: str) -> Path:
    """Resolve a caller-named directory inside the capture base, or raise.

    Same containment rule as `_capture_target`, without the `.png` suffix.
    """
    base = Path(os.environ.get("MESHTASTIC_MCP_CAPTURE_DIR") or Path.cwd()).expanduser().resolve()
    target = (base / Path(out_dir).expanduser()).resolve()
    if not target.is_relative_to(base):
        raise ValueError(
            f"out_dir must be inside {base} (set MESHTASTIC_MCP_CAPTURE_DIR to change it)"
        )
    return target
