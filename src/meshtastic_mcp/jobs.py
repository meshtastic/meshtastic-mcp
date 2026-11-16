# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Background job registry for tools that outlive an MCP request.

Several operations here run far past the typical ~60 s MCP client timeout: a
PlatformIO build, a firmware upload, a vanity-key grind. They all use the same
shape — start a daemon thread, return a `job_id` immediately, poll for status
and a tail of the job's log. This module is that shape, factored out of
`flash.py` so the grinder shares one registry (and one log root) with
build/flash instead of growing a second one.

The registry is process-global and in-memory: jobs do not survive a server
restart, which `poll()` says out loud when an id is unknown.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config

_active: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
"""Guards every mutation of a job's state dict — worker bodies included."""

_ctx = threading.local()


def cancellable_job_running() -> bool:
    """True on a worker thread whose job was started with `cancellable=True`."""
    state = getattr(_ctx, "state", None)
    return bool(state and state.get("cancellable"))


def register_process(proc: subprocess.Popen[str]) -> None:
    """Let `cancel` terminate `proc`. A job thread's subprocess calls this right after spawn."""
    state = getattr(_ctx, "state", None)
    if state is None:
        return
    with LOCK:
        state["proc"] = proc
        requested = state.get("cancel_requested", False)
    if requested:
        _terminate(proc)


def _terminate(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def data_dir(kind: str) -> Path:
    """Log/output directory for jobs of `kind` ("builds", "flashes", "grinds")."""
    d = config.mcp_data_dir() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(
    kind: str,
    label: str,
    worker_body: Callable[[dict[str, Any], Path], None],
    *,
    cancellable: bool = False,
) -> dict[str, Any]:
    """Launch `worker_body(state, log_path)` in a daemon thread, tracked by job_id.

    `kind` names the log subdir; `label` is the human-readable subject of the
    job (a pio env, a grind pattern). `worker_body` owns the actual work and
    updates `state` under `LOCK`.

    `cancellable=True` marks a job that is safe to interrupt (a build); `cancel` refuses the rest
    (a flash must not stop mid-write).
    """
    job_id = uuid.uuid4().hex[:12]
    log_path = data_dir(kind) / f"{job_id}.log"

    state: dict[str, Any] = {
        "job_id": job_id,
        "kind": kind,
        "label": label,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "artifacts": [],
        "log_path": str(log_path),
        "cancellable": cancellable,
    }
    with LOCK:
        _active[job_id] = state

    def _run() -> None:
        _ctx.state = state
        try:
            worker_body(state, log_path)
        except Exception as exc:
            log_path.write_text(f"{kind} worker error: {exc}\n", encoding="utf-8")
            with LOCK:
                state["status"] = "failed"
                state["finished_at"] = time.time()
                state["error"] = str(exc)
        finally:
            _ctx.state = None
            with LOCK:
                state["proc"] = None
                if state.get("cancel_requested") and state["status"] == "failed":
                    state["status"] = "cancelled"
                state["settled"] = True

    threading.Thread(target=_run, daemon=True, name=f"{kind}-{job_id}").start()
    return {"job_id": job_id, "status": "running", "log_path": str(log_path)}


def poll(job_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Status, elapsed time, artifacts and a log tail for a job from `start`."""
    with LOCK:
        state = _active.get(job_id)
    if state is None:
        return {"error": f"Unknown job_id {job_id!r} (only this session's jobs are tracked)."}

    log_path = Path(state["log_path"])
    log_tail: list[str] = []
    if log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]

    with LOCK:
        elapsed = round((state["finished_at"] or time.time()) - state["started_at"], 1)
        return {
            "job_id": job_id,
            "kind": state["kind"],
            "label": state["label"],
            "status": state["status"],
            "elapsed_s": elapsed,
            "exit_code": state.get("exit_code"),
            "duration_s": state.get("duration_s"),
            "artifacts": state.get("artifacts", []),
            # Deliberately NOT "error": a bare {"error": ...} is this module's
            # "unknown job_id" reply, and callers branch on that key's presence.
            "worker_error": state.get("error"),
            "log_tail": log_tail,
            "log_path": state["log_path"],
        }


def cancel(job_id: str) -> dict[str, Any]:
    """Ask a cancellable running job to stop. Non-cancellable jobs keep running and say so."""
    with LOCK:
        state = _active.get(job_id)
        if state is None:
            return {"error": f"Unknown job_id {job_id!r} (only this session's jobs are tracked)."}
        status = state["status"]
        if status != "running":
            return {"job_id": job_id, "status": status, "cancelled": False}
        if not state.get("cancellable"):
            return {
                "job_id": job_id,
                "status": status,
                "cancelled": False,
                "reason": f"a {state['kind']} job cannot be interrupted safely; wait for it",
            }
        state["cancel_requested"] = True
        proc = state.get("proc")
    if proc is not None:
        _terminate(proc)
    return {"job_id": job_id, "status": "running", "cancelled": True}


async def wait(job_id: str, poll_s: float = 0.5, tail_lines: int = 50) -> dict[str, Any]:
    """Await a job's terminal status and return `poll`'s view of it.

    If the awaiting task is cancelled, a cancellable job is cancelled too; any other job keeps
    running and stays pollable by `job_id`.
    """
    try:
        while True:
            with LOCK:
                state = _active.get(job_id)
                settled = state is None or state.get("settled", False)
            if settled:
                break
            await asyncio.sleep(poll_s)
    except asyncio.CancelledError:
        cancel(job_id)
        raise
    return poll(job_id, tail_lines=tail_lines)


async def run_sync(
    kind: str,
    label: str,
    fn: Callable[[], dict[str, Any]],
    *,
    cancellable: bool = False,
) -> dict[str, Any]:
    """Run a blocking function as a tracked job and return its result with the `job_id`.

    The job stays pollable by `job_id` if the awaiting task is cancelled. A function that raises
    makes the job `failed`, and this raises `RuntimeError` with its message.
    """

    def _body(state: dict[str, Any], _log_path: Path) -> None:
        result = fn()
        with LOCK:
            state["result"] = result
            state["exit_code"] = 0
            state["finished_at"] = time.time()
            state["status"] = "done"

    started = start(kind, label, _body, cancellable=cancellable)
    job_id = started["job_id"]
    final = await wait(job_id)
    if final.get("status") != "done":
        raise RuntimeError(
            final.get("worker_error") or f"{kind} job {job_id} {final.get('status')}"
        )
    state = state_of(job_id) or {}
    return {**state["result"], "job_id": job_id}


def state_of(job_id: str) -> dict[str, Any] | None:
    """The raw mutable state dict for `job_id` (read/update under `LOCK`)."""
    with LOCK:
        return _active.get(job_id)


def running_of_kind(kind: str) -> list[dict[str, Any]]:
    """Every still-running job of `kind` — for tools that must find a job they
    did not start (a capture left running by an earlier session's tool call) or
    refuse to start a second one on the same hardware."""
    with LOCK:
        return [s for s in _active.values() if s["kind"] == kind and s["status"] == "running"]
