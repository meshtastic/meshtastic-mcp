# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly bake orchestration.

Three layers in one module:

* ``NightlyConfig`` — operator settings, persisted as a JSON blob in the
  ``settings`` table (key ``"nightly"``), mirroring the Datadog config.
* Pure schedule math (``slot_for`` / ``due_slot`` / ``next_run_at``) — no IO,
  unit-tested directly.
* Git/update step helpers (``firmware_update`` / ``self_update``) — blocking
  subprocess sequences with an injectable command runner; the orchestrator
  dispatches them via ``asyncio.to_thread``.

The nightly owns a dedicated firmware checkout (``nightly_fw_dir``) that it may
hard-reset to the configured branch every night. It never touches whatever
``MESHTASTIC_FIRMWARE_ROOT`` points at unless that IS the nightly checkout —
a mismatch is reported as an observation, not "fixed".
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

from ..db import repo_devices as rd
from ..db import repo_nightly as rn
from ..db import repo_runs as rr
from ..db import repo_settings as rs
from ..db.database import Database
from . import test_runner as tr_mod

log = logging.getLogger("meshtastic_mcp.web.nightly")

SETTINGS_KEY = "nightly"

# Subprocess timeouts (seconds). Module constants so tests can shrink them.
CLONE_TIMEOUT = 2400.0
FETCH_TIMEOUT = 300.0
CHECKOUT_TIMEOUT = 60.0
RESET_TIMEOUT = 120.0
SUBMODULE_TIMEOUT = 900.0
PULL_TIMEOUT = 120.0
PIP_TIMEOUT = 600.0
NPM_TIMEOUT = 900.0


# --- config -----------------------------------------------------------------


@dataclass
class NightlyConfig:
    enabled: bool = False
    hour: int = 1
    minute: int = 30
    self_update: bool = True
    prebuild: bool = True
    force_bake: bool = True
    suite_args: list[str] = field(default_factory=list)
    catchup_window_h: float = 6.0
    suite_timeout_h: float = 4.0
    firmware_branch: str = "develop"
    firmware_url: str = "https://github.com/meshtastic/firmware"
    soak_hours: float = 2.0
    soak_traffic_interval_min: float = 10.0
    soak_snapshot_interval_min: float = 15.0
    soak_keepalive: bool = True
    llm_autostart: bool = False
    recovery_allow_reflash: bool = True
    pipeline_timeout_h: float = 10.0
    keep_nights: int = 30

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> NightlyConfig:
        d = d or {}
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


def coerce_config_patch(base, patch: dict) -> dict:
    """Type-check an incoming config patch against the runtime types of a base
    config instance's fields. Unknown keys are dropped; a value of the wrong
    type raises ValueError (→ HTTP 400) instead of silently persisting a string
    that later crashes the pipeline (e.g. ``soak_hours="2"``)."""
    from dataclasses import fields as _fields

    allowed = {f.name for f in _fields(base)}
    out: dict = {}
    for key, val in patch.items():
        if key not in allowed:
            continue
        cur = getattr(base, key)
        if isinstance(cur, bool):
            if not isinstance(val, bool):
                raise ValueError(f"{key} must be a boolean")
            out[key] = val
        elif isinstance(cur, int) and not isinstance(cur, bool):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or float(val) % 1:
                raise ValueError(f"{key} must be an integer")
            out[key] = int(val)
        elif isinstance(cur, float):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"{key} must be a number")
            out[key] = float(val)
        elif isinstance(cur, list):
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise ValueError(f"{key} must be a list of strings")
            out[key] = val
        elif isinstance(cur, str):
            if not isinstance(val, str):
                raise ValueError(f"{key} must be a string")
            out[key] = val
    return out


def validate_config(cfg: NightlyConfig) -> None:
    """Range checks for a fully-built config. Raises ValueError on violation."""
    if not (0 <= cfg.hour <= 23 and 0 <= cfg.minute <= 59):
        raise ValueError("hour must be 0–23 and minute 0–59")
    if cfg.soak_hours < 0:
        raise ValueError("soak_hours must be ≥ 0")
    if cfg.suite_timeout_h <= 0 or cfg.pipeline_timeout_h <= 0:
        raise ValueError("timeouts must be > 0")
    if cfg.keep_nights < 1:
        raise ValueError("keep_nights must be ≥ 1")
    if cfg.catchup_window_h < 0:
        raise ValueError("catchup_window_h must be ≥ 0")


async def load_config(db: Database) -> NightlyConfig:
    return NightlyConfig.from_dict(await rs.get_json(db, SETTINGS_KEY))


async def save_config(db: Database, cfg: NightlyConfig) -> None:
    await rs.set_json(db, SETTINGS_KEY, asdict(cfg))


# --- paths ------------------------------------------------------------------


def nightly_fw_dir() -> Path:
    """The dedicated firmware checkout the nightly owns (and may hard-reset)."""
    env = os.environ.get("MESHTASTIC_MCP_NIGHTLY_FW_DIR")
    return Path(env) if env else Path.home() / ".meshtastic_mcp" / "nightly-firmware"


def nightly_base_dir() -> Path:
    """Parent of the per-run data dirs (soak logs, snapshots)."""
    return Path.home() / ".meshtastic_mcp" / "nightly"


def nightly_data_dir(nightly_id: int) -> Path:
    return nightly_base_dir() / str(nightly_id)


def mcp_source_root() -> Path | None:
    """The meshtastic-mcp git checkout this process runs from, or None when the
    package is installed without one (wheel install) — self-update then skips.

    src/meshtastic_mcp/web/services/nightly.py -> parents[4] is the repo root.
    """
    env = os.environ.get("MESHTASTIC_MCP_SOURCE_ROOT")
    root = Path(env) if env else Path(__file__).resolve().parents[4]
    if (root / ".git").exists() and (root / "pyproject.toml").exists():
        return root
    return None


# --- schedule math (pure) ---------------------------------------------------


def slot_for(now: datetime, hour: int, minute: int) -> datetime:
    """The most recent wall-clock hh:mm at or before ``now`` (today or
    yesterday). ``now`` must be tz-aware for a stable epoch conversion."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    """The next wall-clock hh:mm strictly after ``now``."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def due_slot(now: datetime, cfg: NightlyConfig, last_scheduled_for: float | None) -> float | None:
    """Epoch of a slot that should start now: within ``catchup_window_h`` of
    its wall-clock time and newer than the last slot ever attempted (any
    outcome — a failed night is not retried until the next slot)."""
    slot = slot_for(now, cfg.hour, cfg.minute)
    ts = slot.timestamp()
    if (now.timestamp() - ts) > cfg.catchup_window_h * 3600.0:
        return None
    if last_scheduled_for is not None and ts <= last_scheduled_for + 1.0:
        return None
    return ts


# --- git/update step helpers (blocking; run via asyncio.to_thread) ----------

# (cmd, cwd, timeout) -> (returncode, stdout, stderr)
CommandRunner = Callable[[list[str], Path | None, float], tuple[int, str, str]]


def _run(cmd: list[str], cwd: Path | None, timeout: float) -> tuple[int, str, str]:
    try:
        out = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return out.returncode, out.stdout, out.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s: {' '.join(cmd)}"


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text[-limit:]


@dataclass
class FwUpdateResult:
    ok: bool
    cloned: bool = False
    sha_before: str | None = None
    sha_after: str | None = None
    error: str | None = None
    untracked: list[str] = field(default_factory=list)


def firmware_update(
    fw_dir: Path, *, url: str, branch: str, runner: CommandRunner = _run
) -> FwUpdateResult:
    """Bring the nightly-owned checkout to the tip of ``origin/<branch>``.

    Clone-if-absent, else fetch + forced checkout + hard reset + submodule
    sync. Deliberately no ``git clean``: ``.pio`` build caches stay warm, and
    the hard reset alone heals a crashed bake's leftover ``userPrefs.jsonc``
    mutation (it is a tracked file). Untracked leftovers are reported, not
    deleted."""
    git = "git"
    # An interrupted first clone leaves a `.git` that is not a usable repo
    # (`rev-parse HEAD` fails). Left alone it would wedge every future night on
    # the fetch/reset; wipe it and re-clone from scratch.
    if (fw_dir / ".git").exists():
        rc, _out, _err = runner([git, "rev-parse", "HEAD"], fw_dir, 10.0)
        if rc != 0:
            shutil.rmtree(fw_dir, ignore_errors=True)
    if not (fw_dir / ".git").exists():
        shutil.rmtree(fw_dir, ignore_errors=True)  # clear a half-clone with no .git
        fw_dir.parent.mkdir(parents=True, exist_ok=True)
        rc, _out, err = runner(
            [git, "clone", "--recurse-submodules", "--branch", branch, url, str(fw_dir)],
            fw_dir.parent,
            CLONE_TIMEOUT,
        )
        if rc != 0:
            return FwUpdateResult(ok=False, cloned=True, error=f"clone: {_tail(err)}")
        rc, out, err = runner([git, "rev-parse", "HEAD"], fw_dir, 10.0)
        sha = out.strip() if rc == 0 else None
        return FwUpdateResult(ok=True, cloned=True, sha_after=sha)

    rc, out, _err = runner([git, "rev-parse", "HEAD"], fw_dir, 10.0)
    sha_before = out.strip() if rc == 0 else None

    steps: list[tuple[str, list[str], float]] = [
        ("fetch", [git, "fetch", "origin"], FETCH_TIMEOUT),
        ("checkout", [git, "checkout", "-f", branch], CHECKOUT_TIMEOUT),
        ("reset", [git, "reset", "--hard", f"origin/{branch}"], RESET_TIMEOUT),
        (
            "submodule",
            [git, "submodule", "update", "--init", "--recursive"],
            SUBMODULE_TIMEOUT,
        ),
    ]
    for name, cmd, timeout in steps:
        rc, _out, err = runner(cmd, fw_dir, timeout)
        if rc != 0:
            return FwUpdateResult(ok=False, sha_before=sha_before, error=f"{name}: {_tail(err)}")

    rc, out, _err = runner([git, "rev-parse", "HEAD"], fw_dir, 10.0)
    sha_after = out.strip() if rc == 0 else None
    rc, out, _err = runner([git, "status", "--porcelain"], fw_dir, 30.0)
    untracked = [ln[3:] for ln in out.splitlines() if ln.startswith("?? ")] if rc == 0 else []
    return FwUpdateResult(ok=True, sha_before=sha_before, sha_after=sha_after, untracked=untracked)


def commit_subjects(
    fw_dir: Path, old: str, new: str, *, limit: int = 20, runner: CommandRunner = _run
) -> list[str]:
    """``<short-sha> <subject>`` lines for old..new, newest first (capped)."""
    rc, out, _err = runner(["git", "log", "--pretty=%h %s", f"{old}..{new}"], fw_dir, 30.0)
    if rc != 0:
        return []
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) > limit:
        return [*lines[:limit], f"… +{len(lines) - limit} more"]
    return lines


@dataclass
class SelfUpdateResult:
    ok: bool
    updated: bool = False
    sha_before: str | None = None
    sha_after: str | None = None
    spa_changed: bool = False
    deps_changed: bool = False
    spa_built: bool = False
    spa_error: str | None = None
    rolled_back: bool = False
    dirty: bool = False
    error: str | None = None


def _swap_dir(fresh: Path, target: Path) -> None:
    """Atomically-enough replace ``target`` with ``fresh`` (same parent)."""
    stale = target.with_name(target.name + ".old")
    if stale.exists():
        shutil.rmtree(stale)
    if target.exists():
        target.rename(stale)
    fresh.rename(target)
    shutil.rmtree(stale, ignore_errors=True)


def self_update(
    root: Path, *, runner: CommandRunner = _run, python: str | None = None
) -> SelfUpdateResult:
    """Fast-forward the meshtastic-mcp checkout to ``origin/master`` and
    reinstall. The SPA is rebuilt only when the diff touches ``web-ui/``, into
    a temp outDir swapped over ``web/static`` so a failed build never destroys
    the served UI. A failed reinstall rolls the checkout back to the previous
    sha (old code keeps running; no restart)."""
    git = "git"
    py = python or sys.executable

    rc, out, err = runner([git, "rev-parse", "HEAD"], root, 10.0)
    if rc != 0:
        return SelfUpdateResult(ok=False, error=f"rev-parse: {_tail(err)}")
    sha_before = out.strip()

    # NEVER touch a dirty checkout. The rollback path below hard-resets to
    # sha_before on a reinstall failure, which would destroy any uncommitted
    # work in the tree — so refuse to self-update at all when it is dirty.
    rc, out, _err = runner([git, "status", "--porcelain"], root, 30.0)
    if rc == 0 and out.strip():
        return SelfUpdateResult(
            ok=False,
            dirty=True,
            sha_before=sha_before,
            error="working tree has uncommitted changes — self-update skipped",
        )

    rc, _out, err = runner([git, "fetch", "origin"], root, PULL_TIMEOUT)
    if rc != 0:
        return SelfUpdateResult(ok=False, sha_before=sha_before, error=f"fetch: {_tail(err)}")

    rc, out, _err = runner([git, "rev-list", "--count", "HEAD..origin/master"], root, 30.0)
    behind = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
    if behind == 0:
        return SelfUpdateResult(ok=True, sha_before=sha_before, sha_after=sha_before)

    rc, out, _err = runner([git, "diff", "--name-only", "HEAD..origin/master"], root, 30.0)
    changed = out.splitlines() if rc == 0 else []
    spa_changed = any(p.startswith("web-ui/") for p in changed)
    deps_changed = any(p in ("pyproject.toml", "uv.lock") for p in changed)
    lock_changed = any(p == "web-ui/package-lock.json" for p in changed)

    rc, _out, err = runner([git, "pull", "--ff-only", "origin", "master"], root, PULL_TIMEOUT)
    if rc != 0:
        return SelfUpdateResult(
            ok=False, sha_before=sha_before, error=f"pull --ff-only: {_tail(err)}"
        )
    rc, out, _err = runner([git, "rev-parse", "HEAD"], root, 10.0)
    sha_after = out.strip() if rc == 0 else None

    rc, _out, err = runner([py, "-m", "pip", "install", "-e", f"{root}[web]"], root, PIP_TIMEOUT)
    if rc != 0:
        # Roll back so the running (old) code and the checkout agree again.
        runner([git, "reset", "--hard", sha_before], root, RESET_TIMEOUT)
        runner([py, "-m", "pip", "install", "-e", f"{root}[web]"], root, PIP_TIMEOUT)
        return SelfUpdateResult(
            ok=False,
            sha_before=sha_before,
            sha_after=sha_after,
            spa_changed=spa_changed,
            deps_changed=deps_changed,
            rolled_back=True,
            error=f"pip install: {_tail(err)}",
        )

    result = SelfUpdateResult(
        ok=True,
        updated=True,
        sha_before=sha_before,
        sha_after=sha_after,
        spa_changed=spa_changed,
        deps_changed=deps_changed,
    )
    if spa_changed:
        ui_dir = root / "web-ui"
        static_dir = root / "src" / "meshtastic_mcp" / "web" / "static"
        fresh = static_dir.with_name(static_dir.name + ".new")
        if lock_changed:
            rc, _out, err = runner(["npm", "ci"], ui_dir, NPM_TIMEOUT)
            if rc != 0:
                result.spa_error = f"npm ci: {_tail(err)}"
                return result
        rc, _out, err = runner(
            ["npm", "run", "build", "--", "--outDir", str(fresh), "--emptyOutDir"],
            ui_dir,
            NPM_TIMEOUT,
        )
        if rc != 0:
            result.spa_error = f"npm run build: {_tail(err)}"
            shutil.rmtree(fresh, ignore_errors=True)
            return result
        try:
            _swap_dir(fresh, static_dir)
            result.spa_built = True
        except OSError as exc:
            result.spa_error = f"static swap: {exc}"
            shutil.rmtree(fresh, ignore_errors=True)
    return result


# --- orchestrator -----------------------------------------------------------

TICK_S = 30.0
GATE_GRACE_S = 1800.0  # how long to wait for a manual run to finish
GATE_POLL_S = 5.0
SUITE_POLL_S = 5.0
BUILD_WAIT_TIMEOUT_S = 3600.0
RESTART_EXIT_WAIT_S = 60.0
RECOVERY_SETTLE_S = 10.0

# Pipeline step order; resume re-enters at the persisted step.
STEPS = (
    "self_update",
    "firmware_update",
    "prebuild",
    "bench_check",
    "suite",
    "soak",
)


class NightlyOrchestrator:
    """Drives the nightly pipeline: schedule tick → step pipeline → report.

    Every hardware-touching stage is delegated to services that already own
    port arbitration (TestRunner, RecoveryService, the soak's guarded calls) —
    the orchestrator itself never opens a serial port. All failure handling
    funnels into ``nightly_observations`` so the report can always be written.
    """

    def __init__(
        self,
        db: Database,
        hub,
        *,
        runner,
        orch,
        serialmon,
        portlocks,
        recovery=None,
        keepalive=None,
        reporter=None,
    ) -> None:
        self.db = db
        self.hub = hub
        self.runner = runner
        self.orch = orch
        self.serialmon = serialmon
        self.portlocks = portlocks
        self.recovery = recovery
        self.keepalive = keepalive
        self.reporter = reporter
        self.cfg = NightlyConfig()
        self._loop_task: asyncio.Task | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._current_id: int | None = None
        self._current_step: str | None = None
        self._resume_checked = False
        # Serializes the check→create-row→launch sequence so a scheduler tick
        # and a manual run_now (each of which awaits mid-sequence) can't both
        # pass the is_pipeline_active() check and launch two pipelines.
        self._launch_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    async def reload(self) -> None:
        self.cfg = await load_config(self.db)

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        # Await the cancellations so pipeline cleanup (DB writes, monitor
        # release) finishes BEFORE shutdown tears down the db/serial monitors.
        tasks = [t for t in (self._loop_task, self._pipeline_task) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        self._pipeline_task = None

    def is_pipeline_active(self) -> bool:
        return self._pipeline_task is not None and not self._pipeline_task.done()

    def status(self) -> dict:
        now = datetime.now().astimezone()
        return {
            "config": self.cfg.to_dict(),
            "state": {
                "active": self.is_pipeline_active(),
                "step": self._current_step if self.is_pipeline_active() else None,
                "nightly_id": self._current_id if self.is_pipeline_active() else None,
                "next_run_at": (
                    next_run_at(now, self.cfg.hour, self.cfg.minute).isoformat()
                    if self.cfg.enabled
                    else None
                ),
            },
        }

    async def run_now(self) -> dict:
        async with self._launch_lock:
            if self.is_pipeline_active():
                raise RuntimeError("a nightly pipeline is already running")
            nid = await rn.create(self.db, scheduled_for=time.time(), trigger="manual")
            self._launch(nid, None)
        return {"nightly_id": nid}

    async def cancel(self) -> None:
        """Graceful: the pipeline notices, marks the night canceled, and still
        runs bench-recover + handoff."""
        if not self.is_pipeline_active():
            return
        self._cancel.set()
        # Stop the suite ONLY when it is the nightly's own run. During the gate
        # (_current_step is None) the bench is held by a pre-existing manual run
        # that cancel must leave alone — the gate loop notices _cancel and
        # aborts on its own.
        if self._current_step == "suite" and tr_mod.is_running():
            try:
                await self.runner.stop()
            except Exception:
                log.debug("runner.stop during cancel failed", exc_info=True)

    # -- scheduler loop ------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly tick failed")
            await asyncio.sleep(TICK_S)

    async def _tick(self) -> None:
        if not self._resume_checked:
            self._resume_checked = True
            if await self._maybe_resume():
                return
        if self.is_pipeline_active():
            return
        if not self.cfg.enabled:
            return
        async with self._launch_lock:
            if self.is_pipeline_active():
                return
            last = await rn.last_scheduled_for(self.db)
            due = due_slot(datetime.now().astimezone(), self.cfg, last)
            if due is None:
                return
            nid = await rn.create(self.db, scheduled_for=due, trigger="schedule")
            self._launch(nid, None)

    async def _maybe_resume(self) -> bool:
        """Continue an unfinished night after a restart (self-update or crash).
        Runs even when the schedule is disabled — an in-flight night finishes."""
        row = await rn.latest_unfinished(self.db)
        if row is None:
            return False
        nid, step = row["id"], row.get("step")
        if row["status"] == "awaiting_restart":
            sha = await self._mcp_head()
            await rn.set_shas(self.db, nid, mcp_after=sha)
            await rn.set_status(self.db, nid, "running")
            await self._observe(
                nid,
                "self_update",
                "info",
                "self_update.restarted",
                f"restarted onto {(sha or 'unknown')[:7]}",
            )
            self._launch(nid, "firmware_update")
            return True
        if step == "suite":
            if row.get("run_id"):
                run = await rr.get_run(self.db, row["run_id"])
                if run and not run.get("finished_at"):
                    await rr.finish_run(self.db, row["run_id"], exit_code=None)
            attempts = await rn.bump_suite_attempts(self.db, nid)
            await self._observe(
                nid, "suite", "error", "suite.interrupted", "process died mid-suite"
            )
            if attempts >= 2:
                await self._observe(
                    nid,
                    "suite",
                    "error",
                    "suite.crash_loop",
                    f"suite died {attempts}× — not flashing the bench again tonight",
                )
                # Straight to finalize (bench_recover + report). "handoff" is not
                # a recognized resume token — using it would re-run the whole
                # pipeline and defeat the crash-loop cap.
                self._launch(nid, "finalize")
                return True
            self._launch(nid, "suite")
            return True
        # A crash after the suite (during bench_recover/handoff) must NOT re-run
        # the whole night — resume straight into finalize (bench_recover +
        # report) with the outcome recomputed from the persisted run.
        if step in ("bench_recover", "handoff", "done"):
            resume_at = "finalize"
        elif step in STEPS:
            resume_at = step
        else:
            resume_at = "self_update"
        await self._observe(
            nid, resume_at, "info", "pipeline.resumed", f"resuming at {resume_at} after restart"
        )
        self._launch(nid, resume_at)
        return True

    # -- pipeline ------------------------------------------------------------

    def _launch(self, nightly_id: int, resume_from: str | None) -> None:
        # Sync + single-threaded → this check is atomic with the task creation
        # below, the last line of defense against a double-launch.
        if self.is_pipeline_active():
            raise RuntimeError("a nightly pipeline is already active")
        self._cancel.clear()
        self._current_id = nightly_id
        self._pipeline_task = asyncio.create_task(self._run_pipeline(nightly_id, resume_from))

    async def _observe(
        self,
        nightly_id: int,
        step: str,
        severity: str,
        kind: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        obs = await rn.add_observation(
            self.db,
            nightly_id,
            step=step,
            severity=severity,
            kind=kind,
            message=message,
            data=data,
        )
        try:
            await self.hub.publish("nightly.update", {"type": "observation", **obs})
        except Exception:
            log.debug("nightly observation publish failed", exc_info=True)

    async def _set_step(self, nightly_id: int, step: str) -> None:
        self._current_step = step
        await rn.set_step(self.db, nightly_id, step)
        try:
            await self.hub.publish(
                "nightly.update", {"type": "step", "nightly_id": nightly_id, "step": step}
            )
        except Exception:
            log.debug("nightly step publish failed", exc_info=True)

    async def _run_pipeline(self, nightly_id: int, resume_from: str | None) -> None:
        outcome = "error"
        cancelled = False
        try:
            try:
                outcome = await asyncio.wait_for(
                    self._main_steps(nightly_id, resume_from),
                    timeout=self.cfg.pipeline_timeout_h * 3600.0,
                )
            except TimeoutError:
                await self._observe(
                    nightly_id,
                    "pipeline",
                    "error",
                    "pipeline.timeout",
                    f"pipeline exceeded {self.cfg.pipeline_timeout_h}h — aborting",
                )
                if tr_mod.is_running():
                    try:
                        await self.runner.stop()
                    except Exception:
                        log.debug("runner.stop on timeout failed", exc_info=True)
        except asyncio.CancelledError:
            # Process shutdown (incl. our own self-update restart). The night is
            # NOT finalized here — _maybe_resume picks it up on the next boot.
            cancelled = True
            raise
        except Exception as exc:
            await self._observe(nightly_id, "pipeline", "error", "pipeline.exception", repr(exc))
        finally:
            self._current_step = None
            if not cancelled:
                try:
                    row = await rn.get(self.db, nightly_id)
                    # An awaiting_restart night continues after the respawn —
                    # no bench-recover/handoff yet.
                    if row is not None and row["status"] != "awaiting_restart":
                        await self._finalize(nightly_id, outcome)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("nightly finalize failed for %s", nightly_id)

    async def _finalize(self, nightly_id: int, outcome: str) -> None:
        await self._bench_recover(nightly_id)
        await self._set_step(nightly_id, "handoff")
        if self._cancel.is_set():
            outcome = "canceled"
        summary = await self._summary(nightly_id)
        await rn.finish(self.db, nightly_id, status=outcome, summary=summary)
        if self.reporter is not None:
            try:
                await self.reporter.publish(nightly_id)
            except Exception as exc:
                await self._observe(nightly_id, "handoff", "error", "report.failed", repr(exc))
        await self._retention_prune(nightly_id)
        await rn.set_step(self.db, nightly_id, "done")
        try:
            await self.hub.publish(
                "nightly.update",
                {"type": "finished", "nightly_id": nightly_id, "status": outcome},
            )
        except Exception:
            log.debug("nightly finished publish failed", exc_info=True)

    async def _summary(self, nightly_id: int) -> dict | None:
        row = await rn.get(self.db, nightly_id)
        if row is None or not row.get("run_id"):
            return None
        run = await rr.get_run(self.db, row["run_id"])
        if run is None:
            return None
        return {
            "passed": run.get("passed", 0),
            "failed": run.get("failed", 0),
            "skipped": run.get("skipped", 0),
            "exit_code": run.get("exit_code"),
        }

    async def _persisted_suite_ok(self, nightly_id: int) -> bool:
        """The suite verdict from the linked run row — the source of truth when
        the suite step itself was skipped (resume-at-soak/finalize). True when
        no suite was ever linked (nothing to have failed on)."""
        row = await rn.get(self.db, nightly_id)
        if row is None or not row.get("run_id"):
            return True
        run = await rr.get_run(self.db, row["run_id"])
        return run is None or run.get("exit_code") == 0

    async def _main_steps(self, nightly_id: int, resume_from: str | None) -> str:
        """Returns the night's outcome: passed | failed | error | canceled."""
        # Post-suite crash: everything up to and including the soak is done; the
        # finally will run bench_recover + handoff. Recompute the verdict here.
        if resume_from == "finalize":
            return "passed" if await self._persisted_suite_ok(nightly_id) else "failed"
        skipping = resume_from is not None and resume_from in STEPS

        async def should_run(step: str) -> bool:
            nonlocal skipping
            if skipping:
                if step == resume_from:
                    skipping = False
                else:
                    return False
            await self._set_step(nightly_id, step)
            return True

        if not await self._gate(nightly_id):
            return "error"

        if await should_run("self_update"):
            await self._step_self_update(nightly_id)
        if self._cancel.is_set():
            return "canceled"

        fw_ok = True
        if await should_run("firmware_update"):
            fw_ok = await self._step_firmware_update(nightly_id)
        if not fw_ok:
            return "error"
        if self._cancel.is_set():
            return "canceled"

        if await should_run("prebuild"):
            await self._step_prebuild(nightly_id)
        if self._cancel.is_set():
            return "canceled"

        if await should_run("bench_check"):
            await self._step_bench_check(nightly_id)
        if self._cancel.is_set():
            return "canceled"

        ran_suite = False
        suite_ok = True
        if await should_run("suite"):
            if self._cancel.is_set():
                return "canceled"
            ran_suite = True
            suite_ok = await self._step_suite(nightly_id)

        if await should_run("soak") and not self._cancel.is_set() and self.cfg.soak_hours > 0:
            await self._step_soak(nightly_id)

        if self._cancel.is_set():
            return "canceled"
        # When the suite step was skipped on resume, its verdict lives in the
        # persisted run, not the (default-True) suite_ok local.
        if not ran_suite:
            suite_ok = await self._persisted_suite_ok(nightly_id)
        return "passed" if suite_ok else "failed"

    # -- steps ---------------------------------------------------------------

    async def _gate(self, nightly_id: int) -> bool:
        """Wait for a manual test run to clear the bench; self-heal a wedged
        runner whose pytest child died without reporting."""
        deadline = time.monotonic() + GATE_GRACE_S
        while tr_mod.is_running():
            st = tr_mod.status()
            elapsed = st.get("elapsed_s") or 0
            if st.get("last_line") is None and elapsed > 600:
                try:
                    await self.runner.reset()
                except Exception:
                    log.debug("runner.reset failed", exc_info=True)
                await self._observe(
                    nightly_id,
                    "gate",
                    "warn",
                    "runner.force_reset",
                    f"cleared a silent test run wedged for {elapsed:.0f}s",
                )
                break
            if time.monotonic() > deadline:
                await self._observe(
                    nightly_id,
                    "gate",
                    "error",
                    "bench.busy",
                    "a test run still held the bench after the grace window — "
                    "skipping tonight's pipeline",
                )
                return False
            if self._cancel.is_set():
                return False
            await asyncio.sleep(GATE_POLL_S)
        return True

    async def _mcp_head(self) -> str | None:
        root = mcp_source_root()
        if root is None:
            return None
        rc, out, _err = await asyncio.to_thread(_run, ["git", "rev-parse", "HEAD"], root, 10.0)
        return out.strip() if rc == 0 else None

    async def _step_self_update(self, nightly_id: int) -> None:
        t0 = time.monotonic()
        if not self.cfg.self_update:
            await self._observe(
                nightly_id, "self_update", "info", "step.finished", "self-update disabled"
            )
            return
        root = mcp_source_root()
        if root is None:
            await self._observe(
                nightly_id,
                "self_update",
                "warn",
                "self_update.no_checkout",
                "meshtastic-mcp is not running from a git checkout — self-update skipped",
            )
            return
        res = await asyncio.to_thread(self_update, root)
        await rn.set_shas(self.db, nightly_id, mcp_before=res.sha_before, mcp_after=res.sha_after)
        duration = time.monotonic() - t0
        if res.dirty:
            await self._observe(
                nightly_id,
                "self_update",
                "warn",
                "self_update.dirty_skip",
                "meshtastic-mcp checkout has uncommitted changes — self-update "
                "skipped to protect them; running on the existing code",
                {"duration_s": duration},
            )
            return
        if res.rolled_back:
            await self._observe(
                nightly_id,
                "self_update",
                "error",
                "self_update.rolled_back",
                f"update failed and was rolled back: {res.error}",
                {"duration_s": duration},
            )
            return
        if not res.ok:
            await self._observe(
                nightly_id,
                "self_update",
                "error",
                "self_update.failed",
                res.error or "self-update failed",
                {"duration_s": duration},
            )
            return
        if res.spa_error:
            await self._observe(
                nightly_id,
                "self_update",
                "warn",
                "self_update.spa_failed",
                f"SPA rebuild failed (old UI keeps serving): {res.spa_error}",
            )
        await self._observe(
            nightly_id,
            "self_update",
            "info",
            "step.finished",
            "updated" if res.updated else "already up to date",
            {"duration_s": duration},
        )
        if res.updated:
            await self._restart(nightly_id)

    async def _restart(self, nightly_id: int) -> None:
        await rn.set_status(self.db, nightly_id, "awaiting_restart")
        await self._observe(
            nightly_id,
            "self_update",
            "info",
            "self_update.restarting",
            "new code pulled — restarting FleetSuite (launchd respawns it)",
        )
        await asyncio.sleep(1.0)  # let the observation/WS frame flush
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(RESTART_EXIT_WAIT_S)
        # Still alive → not running under launchd (or SIGTERM was trapped).
        await rn.set_status(self.db, nightly_id, "running")
        await self._observe(
            nightly_id,
            "self_update",
            "warn",
            "self_update.restart_failed",
            "SIGTERM did not stop the process — continuing on the OLD code "
            "(is FleetSuite running under launchd?)",
        )

    async def _step_firmware_update(self, nightly_id: int) -> bool:
        t0 = time.monotonic()
        fw_dir = nightly_fw_dir()
        from meshtastic_mcp import config as mcfg

        active_root = mcfg.firmware_root_or_none()
        if active_root is not None and active_root.resolve() != fw_dir.resolve():
            await self._observe(
                nightly_id,
                "firmware_update",
                "warn",
                "firmware.root_mismatch",
                f"MESHTASTIC_FIRMWARE_ROOT is {active_root}, but the nightly "
                f"checkout is {fw_dir} — builds/bakes will use the former",
            )
        res = await asyncio.to_thread(
            firmware_update,
            fw_dir,
            url=self.cfg.firmware_url,
            branch=self.cfg.firmware_branch,
        )
        duration = time.monotonic() - t0
        await rn.set_shas(
            self.db,
            nightly_id,
            fw_before=res.sha_before,
            fw_after=res.sha_after or res.sha_before,
        )
        if res.cloned and res.ok:
            await self._observe(
                nightly_id,
                "firmware_update",
                "info",
                "firmware.cloned",
                f"first clone of {self.cfg.firmware_url} took {duration:.0f}s",
            )
        if not res.ok:
            if res.cloned:
                await self._observe(
                    nightly_id,
                    "firmware_update",
                    "error",
                    "firmware.clone_failed",
                    f"clone failed — no firmware to test: {res.error}",
                    {"duration_s": duration},
                )
                return False
            await self._observe(
                nightly_id,
                "firmware_update",
                "error",
                "firmware.fetch_failed",
                f"update failed — suite runs on the existing checkout: {res.error}",
                {"duration_s": duration},
            )
            return True
        if res.untracked:
            await self._observe(
                nightly_id,
                "firmware_update",
                "info",
                "firmware.untracked_files",
                f"{len(res.untracked)} untracked file(s) left in the checkout",
                {"files": res.untracked[:20]},
            )
        await self._observe(
            nightly_id,
            "firmware_update",
            "info",
            "step.finished",
            f"develop at {(res.sha_after or '?')[:7]}",
            {"duration_s": duration},
        )
        return True

    async def _step_prebuild(self, nightly_id: int) -> None:
        if not self.cfg.prebuild:
            await self._observe(
                nightly_id, "prebuild", "info", "step.finished", "prebuild disabled"
            )
            return
        t0 = time.monotonic()
        row = await rn.get(self.db, nightly_id)
        sha = (row or {}).get("fw_sha_after")
        if not sha:
            await self._observe(
                nightly_id, "prebuild", "warn", "build.no_sha", "no firmware sha — skipped"
            )
            return
        from . import control

        fleet = await rd.online_with_env(self.db)
        envs = sorted({e for e in (control.env_for_device(d) for d in fleet) if e})
        if not envs:
            await self._observe(
                nightly_id, "prebuild", "warn", "build.no_envs", "no online fleet envs to build"
            )
            return
        await self.orch.enqueue(envs, sha=sha, branch=self.cfg.firmware_branch)
        rows = await self.orch.wait(envs, sha=sha, timeout_s=BUILD_WAIT_TIMEOUT_S)
        failed = [r for r in rows if r.get("status") not in ("success", "cached")]
        for r in failed:
            await self._observe(
                nightly_id,
                "prebuild",
                "error",
                "build.failed",
                f"{r.get('env')}: prebuild {r.get('status')} — the bake will "
                "compile it itself (and fail loudly if it can't)",
                {"env": r.get("env"), "error": (r.get("error") or "")[-500:]},
            )
        await self._observe(
            nightly_id,
            "prebuild",
            "info",
            "step.finished" if not failed else "step.failed",
            f"{len(rows) - len(failed)}/{len(rows)} envs built",
            {"duration_s": time.monotonic() - t0},
        )

    async def _expected_fleet(self) -> list[dict]:
        return [d for d in await rd.list_all(self.db) if d.get("kind") == "usb" and d.get("env")]

    async def _recover_device(
        self, nightly_id: int, step: str, device: dict, *, allow_reflash: bool
    ) -> None:
        serial = device.get("serial_number")
        if self.recovery is None or not serial:
            return
        label = device.get("friendly_name") or serial
        try:
            await self._observe(
                nightly_id,
                step,
                "info",
                "recovery.attempted",
                f"{label}: running recovery ladder (reflash={'on' if allow_reflash else 'off'})",
                {"serial": serial},
            )
            report = await self.recovery.recover(
                serial, allow_reflash=allow_reflash, confirm=allow_reflash
            )
            ok = bool(report.get("recovered") or report.get("ok"))
            await self._observe(
                nightly_id,
                step,
                "info" if ok else "warn",
                "recovery.succeeded" if ok else "recovery.failed",
                f"{label}: recovery {'succeeded' if ok else 'did not revive the device'}",
                {"serial": serial},
            )
        except Exception as exc:
            await self._observe(
                nightly_id,
                step,
                "warn",
                "recovery.failed",
                f"{label}: recovery errored: {exc}",
                {"serial": serial},
            )

    async def _step_bench_check(self, nightly_id: int) -> None:
        offline = [d for d in await self._expected_fleet() if not d.get("online")]
        if not offline:
            await self._observe(
                nightly_id, "bench_check", "info", "step.finished", "all expected devices online"
            )
            return
        for device in offline:
            await self._recover_device(nightly_id, "bench_check", device, allow_reflash=False)
        await asyncio.sleep(RECOVERY_SETTLE_S)  # let discovery re-enumerate
        still_offline = [d for d in await self._expected_fleet() if not d.get("online")]
        await self._observe(
            nightly_id,
            "bench_check",
            "info" if not still_offline else "warn",
            "step.finished" if not still_offline else "step.failed",
            f"{len(offline) - len(still_offline)}/{len(offline)} offline devices revived",
        )

    async def _step_suite(self, nightly_id: int) -> bool:
        t0 = time.monotonic()
        args = (["--force-bake"] if self.cfg.force_bake else []) + list(self.cfg.suite_args)
        started = None
        for attempt in (1, 2):
            try:
                started = await self.runner.start(args)
                break
            except RuntimeError as exc:
                if attempt == 2:
                    await self._observe(
                        nightly_id, "suite", "error", "suite.start_failed", str(exc)
                    )
                    return False
                await asyncio.sleep(60.0)
        run_id = (started or {}).get("run_id") or tr_mod.status().get("run_id")
        if run_id:
            await rn.set_run_id(self.db, nightly_id, run_id)
        deadline = time.monotonic() + self.cfg.suite_timeout_h * 3600.0
        while tr_mod.is_running():
            if self._cancel.is_set():
                return False
            if time.monotonic() > deadline:
                await self._observe(
                    nightly_id,
                    "suite",
                    "error",
                    "suite.timeout",
                    f"suite exceeded {self.cfg.suite_timeout_h}h — stopping it",
                )
                try:
                    await self.runner.stop()
                except Exception:
                    log.debug("runner.stop on suite timeout failed", exc_info=True)
                return False
            await asyncio.sleep(SUITE_POLL_S)
        duration = time.monotonic() - t0
        run = await rr.get_run(self.db, run_id) if run_id else None
        exit_code = (run or {}).get("exit_code")
        ok = exit_code == 0
        await self._observe(
            nightly_id,
            "suite",
            "info" if ok else "error",
            "step.finished" if ok else "step.failed",
            f"suite exit {exit_code}: {(run or {}).get('passed', '?')} passed, "
            f"{(run or {}).get('failed', '?')} failed",
            {"duration_s": duration},
        )
        return ok

    async def _step_soak(self, nightly_id: int) -> None:
        from .nightly_soak import NightlySoak  # local import — avoids a module cycle

        t0 = time.monotonic()
        row = await rn.get(self.db, nightly_id)
        soak_started = (row or {}).get("soak_started_at")
        total = self.cfg.soak_hours * 3600.0
        if soak_started:
            remaining = total - (time.time() - soak_started)  # resumed mid-soak
        else:
            await rn.set_soak_started(self.db, nightly_id, time.time())
            remaining = total
        if remaining <= 0:
            await self._observe(
                nightly_id, "soak", "info", "step.finished", "soak window already elapsed"
            )
            return

        async def observe(severity: str, kind: str, message: str, data: dict | None) -> None:
            await self._observe(nightly_id, "soak", severity, kind, message, data)

        soak = NightlySoak(
            self.db,
            self.serialmon,
            self.portlocks,
            cfg=self.cfg,
            nightly_id=nightly_id,
            data_dir=nightly_data_dir(nightly_id),
            observe=observe,
            keepalive=self.keepalive,
        )
        summary = await soak.run(remaining, cancel=self._cancel)
        await self._observe(
            nightly_id,
            "soak",
            "info",
            "soak.summary",
            f"soak captured {sum(summary.lines.values())} lines from "
            f"{len(summary.lines)} device(s)",
            summary.as_dict(),
        )
        await self._observe(
            nightly_id,
            "soak",
            "info",
            "step.finished",
            "soak complete",
            {"duration_s": time.monotonic() - t0},
        )

    async def _bench_recover(self, nightly_id: int) -> None:
        await self._set_step(nightly_id, "bench_recover")
        try:
            candidates = [
                d
                for d in await self._expected_fleet()
                if not d.get("online") or self.serialmon.is_wedged(d.get("serial_number") or "")
            ]
            if not candidates:
                await self._observe(
                    nightly_id, "bench_recover", "info", "step.finished", "bench healthy"
                )
                return
            for device in candidates:
                await self._recover_device(
                    nightly_id,
                    "bench_recover",
                    device,
                    allow_reflash=self.cfg.recovery_allow_reflash,
                )
            await self._observe(
                nightly_id,
                "bench_recover",
                "info",
                "step.finished",
                f"recovery attempted on {len(candidates)} device(s)",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._observe(
                nightly_id, "bench_recover", "warn", "bench_recover.exception", repr(exc)
            )

    async def _retention_prune(self, nightly_id: int) -> None:
        """Bound disk growth: old per-night data dirs, and artifact trees for
        firmware shas only OLD nightly runs reference (never a sha a recent
        night — or tonight — used, so manual daytime work is untouched)."""
        try:
            keep = max(1, self.cfg.keep_nights)
            rows = await rn.list_runs(self.db, limit=1000)
            recent, old = rows[:keep], rows[keep:]
            pruned_dirs = 0
            for row in old:
                d = nightly_data_dir(row["id"])
                if d.is_dir():
                    await asyncio.to_thread(shutil.rmtree, d, True)
                    pruned_dirs += 1
            recent_shas = {r.get("fw_sha_after") for r in recent} | {
                r.get("fw_sha_before") for r in recent
            }
            old_shas: set[str] = {
                sha
                for sha in (r.get("fw_sha_after") for r in old)
                if sha and sha not in recent_shas
            }
            from . import builder

            pruned_artifacts = 0
            for sha in old_shas:
                d = builder.artifact_dir(sha, "x").parent  # <root>/<sha>
                if d.is_dir():
                    await asyncio.to_thread(shutil.rmtree, d, True)
                    pruned_artifacts += 1
            if pruned_dirs or pruned_artifacts:
                await self._observe(
                    nightly_id,
                    "handoff",
                    "info",
                    "retention.pruned",
                    f"pruned {pruned_dirs} old night dir(s) and "
                    f"{pruned_artifacts} stale artifact tree(s)",
                )
        except Exception:
            log.debug("retention prune failed", exc_info=True)
