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
restart, which `poll()` says out loud when an id is unknown. Under
`meshtastic-mcp serve` every client shares it.

A job's thread records its progress with `report`, and a caller awaiting the job
with `wait(on_progress=...)` forwards it as MCP progress notifications.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

_active: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
"""Guards every mutation of a job's state dict — worker bodies included."""

_ctx = threading.local()

PROGRESS_INTERVAL_S = 1.0
"""The least time between two progress notifications to one caller, however chatty the tool."""

_PROGRESS_STEP = 0.01

ProgressSend = Callable[[float, float | None, str | None], Awaitable[None]]
"""`Context.report_progress`'s shape: `(progress, total, message)`."""


class Progress:
    """The latest progress of blocking work, set by the thread doing it and read by its waiter.

    `done` and `total` count work in the tool's own units (bytes, percent points, packets); an
    update with only a message keeps the last count.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._message = ""
        self._done: float | None = None
        self._total: float | None = None

    def update(self, message: str, done: float | None = None, total: float | None = None) -> None:
        with self._lock:
            self._seq += 1
            self._message = message
            if done is not None:
                self._done, self._total = done, total

    def snapshot(self) -> tuple[int, str, float | None, float | None]:
        with self._lock:
            return self._seq, self._message, self._done, self._total


def current_progress() -> Progress | None:
    """The `Progress` this thread reports into: its job's, or a `tracking` block's."""
    return getattr(_ctx, "progress", None)


def report(message: str, done: float | None = None, total: float | None = None) -> None:
    """Record progress for the job or `tracking` block on this thread; a no-op anywhere else."""
    progress = current_progress()
    if progress is not None:
        progress.update(message, done, total)


@contextmanager
def tracking(progress: Progress) -> Iterator[None]:
    """Send `report` calls made on this thread to `progress` for the length of the block."""
    previous = current_progress()
    _ctx.progress = progress
    try:
        yield
    finally:
        _ctx.progress = previous


class _Forwarder:
    """Turns `Progress` snapshots into notifications that are throttled and only ever grow."""

    def __init__(self, send: ProgressSend) -> None:
        self._send: ProgressSend | None = send
        self._seq = 0
        self._sent_at = -math.inf
        self._value = 0.0
        self._extra = 0.0

    async def __call__(self, progress: Progress) -> None:
        seq, message, done, total = progress.snapshot()
        now = time.monotonic()
        if self._send is None or seq == self._seq or now - self._sent_at < PROGRESS_INTERVAL_S:
            return
        self._seq, self._sent_at = seq, now
        value = (done or 0.0) + self._extra
        if value <= self._value:
            # MCP progress must grow with every notification, so an update that only changes
            # the message counts as a sliver of work, in the total too.
            self._extra += self._value + _PROGRESS_STEP - value
            value = self._value + _PROGRESS_STEP
        self._value = value
        try:
            await self._send(value, None if total is None else total + self._extra, message or None)
        except Exception as exc:  # the caller still gets its result
            log.debug("progress notification failed, sending no more: %s", exc)
            self._send = None


def forward_progress(progress: Progress, send: ProgressSend, poll_s: float = 0.5) -> asyncio.Task:
    """Forward `progress` to `send` from a task on the running loop until that task is cancelled."""

    async def _loop() -> None:
        forward = _Forwarder(send)
        while True:
            await asyncio.sleep(poll_s)
            await forward(progress)

    return asyncio.ensure_future(_loop())


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
        "progress": Progress(),
    }
    with LOCK:
        _active[job_id] = state

    def _run() -> None:
        _ctx.state = state
        _ctx.progress = state["progress"]
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
            _ctx.progress = None
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
        return {"error": f"Unknown job_id {job_id!r}; jobs end when the server restarts."}

    with LOCK:
        elapsed = round((state["finished_at"] or time.time()) - state["started_at"], 1)
        result: dict[str, Any] = {
            "job_id": job_id,
            "kind": state["kind"],
            "label": state["label"],
            "status": state["status"],
            "elapsed_s": elapsed,
            "exit_code": state.get("exit_code"),
            "duration_s": state.get("duration_s"),
            "artifacts": list(state.get("artifacts", [])),
            # Deliberately NOT "error": a bare {"error": ...} is this module's
            # "unknown job_id" reply, and callers branch on that key's presence.
            "worker_error": state.get("error"),
            "log_path": state["log_path"],
        }

    # The log is read after the status: a job writes its log before it leaves "running", so a
    # final status always comes with the whole log.
    log_path = Path(result["log_path"])
    log_tail: list[str] = []
    if log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]
    result["log_tail"] = log_tail
    return result


def cancel(job_id: str) -> dict[str, Any]:
    """Ask a cancellable running job to stop. Non-cancellable jobs keep running and say so."""
    with LOCK:
        state = _active.get(job_id)
        if state is None:
            return {"error": f"Unknown job_id {job_id!r}; jobs end when the server restarts."}
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
        # Signal under the lock and only a live process: once it has exited and been reaped,
        # its pid (the process group id) can belong to something else.
        if proc is not None and proc.poll() is None:
            _terminate(proc)
    return {"job_id": job_id, "status": "running", "cancelled": True}


async def wait(
    job_id: str,
    poll_s: float = 0.5,
    tail_lines: int = 50,
    *,
    cancel_job: bool = True,
    on_progress: ProgressSend | None = None,
) -> dict[str, Any]:
    """Await a job's terminal status and return `poll`'s view of it.

    If the awaiting task is cancelled and `cancel_job` is set, a cancellable job is cancelled too;
    otherwise the job keeps running and stays pollable by `job_id`. `on_progress` gets what the
    job reports, at most once per `PROGRESS_INTERVAL_S`; a failure there stops the notifications,
    never the wait.
    """
    forward = _Forwarder(on_progress) if on_progress is not None else None
    try:
        while True:
            with LOCK:
                state = _active.get(job_id)
                settled = state is None or state.get("settled", False)
            if settled:
                break
            progress = state.get("progress") if state is not None else None
            if forward is not None and progress is not None:
                await forward(progress)
            await asyncio.sleep(poll_s)
    except asyncio.CancelledError:
        if cancel_job:
            cancel(job_id)
        raise
    return poll(job_id, tail_lines=tail_lines)


async def run_sync(
    kind: str,
    label: str,
    fn: Callable[[], dict[str, Any]],
    *,
    cancellable: bool = False,
    on_started: Callable[[str], Awaitable[None]] | None = None,
    on_progress: ProgressSend | None = None,
) -> dict[str, Any]:
    """Run a blocking function as a tracked job and return its result with the `job_id`.

    The job stays pollable by `job_id` if the awaiting task is cancelled; `on_started` receives
    that id before the wait begins, so a caller whose task is cancelled still has it. A function
    that raises makes the job `failed`, and this raises `RuntimeError` with its message.
    `on_progress` is `wait`'s.
    """

    def _body(state: dict[str, Any], log_path: Path) -> None:
        result = fn()
        code = result.get("exit_code")
        failed = code not in (0, None) or bool(result.get("upload_port_mismatch"))
        # The function's own output, so a poll by job_id shows what the awaiting call saw.
        streams = [str(result.get(k) or "") for k in ("stdout_tail", "stderr_tail")]
        log_path.write_text("\n".join(s for s in streams if s), encoding="utf-8")
        with LOCK:
            state["result"] = result
            state["exit_code"] = code if code is not None else (1 if failed else 0)
            state["finished_at"] = time.time()
            state["status"] = "failed" if failed else "done"

    started = start(kind, label, _body, cancellable=cancellable)
    job_id = started["job_id"]
    if on_started is not None:
        await on_started(job_id)
    final = await wait(job_id, on_progress=on_progress)
    state = state_of(job_id) or {}
    if "result" not in state:
        raise RuntimeError(
            final.get("worker_error") or f"{kind} job {job_id} {final.get('status')}"
        )
    # A failed result comes back as-is with its job_id; the caller decides it is an error.
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
