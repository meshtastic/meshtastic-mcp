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


def data_dir(kind: str) -> Path:
    """Log/output directory for jobs of `kind` ("builds", "flashes", "grinds")."""
    d = config.mcp_data_dir() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(
    kind: str,
    label: str,
    worker_body: Callable[[dict[str, Any], Path], None],
) -> dict[str, Any]:
    """Launch `worker_body(state, log_path)` in a daemon thread, tracked by job_id.

    `kind` names the log subdir; `label` is the human-readable subject of the
    job (a pio env, a grind pattern). `worker_body` owns the actual work and
    updates `state` under `LOCK`.
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
    }
    with LOCK:
        _active[job_id] = state

    def _run() -> None:
        try:
            worker_body(state, log_path)
        except Exception as exc:
            log_path.write_text(f"{kind} worker error: {exc}\n", encoding="utf-8")
            with LOCK:
                state["status"] = "failed"
                state["finished_at"] = time.time()
                state["error"] = str(exc)

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


def state_of(job_id: str) -> dict[str, Any] | None:
    """The raw mutable state dict for `job_id` (read/update under `LOCK`)."""
    with LOCK:
        return _active.get(job_id)
