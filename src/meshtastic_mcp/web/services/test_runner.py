"""The pytest runner.

``resolve_env_overrides`` and ``is_running`` are the pure pieces the safety
gate and the run-launcher depend on. ``TestRunner`` drives an actual pytest
subprocess: it bakes per-board env overrides, tails ``pytest-reportlog`` JSONL
for live per-test progress, and streams stdout/stderr + firmware logs over the
hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("meshtastic_mcp.web.test_runner")

# A healthy pytest is never silent this long — it prints the session header and
# starts writing the reportlog within seconds (even the easyocr/torch import for
# the ui tier is well under a minute). If NOTHING appears in this window the
# subprocess launch wedged (see build_pytest_invocation), so the watchdog kills
# it rather than letting the run hang forever.
STARTUP_SILENCE_TIMEOUT = 240.0

# A healthy subprocess spawn returns near-instantly. If create_subprocess_exec
# itself parks this long the launch wedged (e.g. an asyncio-subprocess / fork
# issue) — fail the run visibly instead of hanging forever.
SPAWN_TIMEOUT = 60.0


def build_pytest_invocation(
    report: Path, args: list[str], root: Path
) -> tuple[list[str], Path | None, dict[str, str]]:
    """Build the (argv, cwd, env_overrides) for launching pytest.

    On macOS this is subtle. FleetSuite's desktop mode runs pywebview
    (Cocoa/WebKit) on the process *main thread* while uvicorn/asyncio runs on a
    daemon thread. Launching a subprocess with ``cwd`` set forces CPython down
    the ``fork``+``exec`` path — and forking a process that has initialised
    CoreFoundation / Objective-C / AVFoundation (which pywebview + the camera
    capture both do) hits the macOS fork-safety hazard: the child wedges before
    ``exec`` and pytest never starts. So on darwin we spawn ``/bin/sh`` with
    ``cwd=None`` (which lets CPython use ``posix_spawn`` — no fork) and do the
    chdir inside the clean child shell, ``exec``-ing pytest so the process we
    track *is* pytest. Belt-and-suspenders: disable the ObjC fork-safety abort.
    """
    inner = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        f"--report-log={report}",
        "-v",
        # Stream subprocess output (pio compile, esptool flash, …) live instead
        # of capturing it, so a multi-minute bake shows real progress in the UI
        # console rather than looking frozen.
        "--capture=no",
        *args,
    ]
    if sys.platform == "darwin":
        shell_cmd = (
            "cd " + shlex.quote(str(root)) + " && exec " + " ".join(shlex.quote(c) for c in inner)
        )
        return (
            ["/bin/sh", "-c", shell_cmd],
            None,
            {"OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"},
        )
    return (inner, root, {})


# Tiers the UI knows about, in display order. A nodeid maps to a tier by its
# path under tests/ (directory name, or "bake"/"unit" for top-level files).
TIERS = (
    "bake",
    "unit",
    "mesh",
    "telemetry",
    "monitor",
    "fleet",
    "admin",
    "provisioning",
    "recovery",
    "ui",
)

# Module-level run state. A single harness runs one suite at a time.
# ``nodeid``/``since``/``last_line`` track the in-flight test so the UI can show
# a live "still working" heartbeat (elapsed + current test + last output line)
# even during a single multi-minute test like a bake compile.
_state: dict = {
    "running": False,
    "run_id": None,
    "exit_code": None,
    "proc": None,
    "nodeid": None,
    "since": None,
    "last_line": None,
}

# How often the runner emits a heartbeat for the in-flight test.
HEARTBEAT_S = 3.0


def is_running() -> bool:
    return bool(_state.get("running"))


def _elapsed_s() -> float | None:
    since = _state.get("since")
    return round(time.time() - since, 1) if since else None


def status() -> dict:
    return {
        "running": _state["running"],
        "run_id": _state["run_id"],
        "exit_code": _state["exit_code"],
        "nodeid": _state.get("nodeid"),
        "elapsed_s": _elapsed_s(),
        "last_line": _state.get("last_line"),
    }


_bench_mod = None
_bench_tried = False


def _repo_root() -> Path:
    """The meshtastic-mcp repo root (where ``tests/`` lives).

    src/meshtastic_mcp/web/services/test_runner.py -> parents[4] is the root.
    A standalone helper so it's monkeypatchable in tests and shared by the
    bench-registry import and the pytest working directory below.
    """
    return Path(__file__).resolve().parents[4]


def _load_bench():
    """Import the canonical bench registry (``tests/_bench.py``) — the single
    source of truth for the per-board role↔hub-slot↔env mapping.

    It lives under ``tests/`` which is NOT an installed package, so a bare
    ``import`` only works when the repo root is on ``sys.path``. In the running
    web server we add the repo root (derived from this file's location) on
    demand. Returns None if it can't be located — callers then fall back to the
    coarse VID role, so a missing bench registry degrades gracefully instead of
    breaking a run."""
    global _bench_mod, _bench_tried
    if _bench_tried:
        return _bench_mod
    _bench_tried = True
    try:
        from tests import _bench  # type: ignore

        _bench_mod = _bench
        return _bench_mod
    except Exception:
        pass
    try:
        root = str(_repo_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        from tests import _bench  # type: ignore

        _bench_mod = _bench
    except Exception:
        log.debug("bench registry unavailable; using coarse VID role keys")
        _bench_mod = None
    return _bench_mod


def resolve_env_overrides(rows: list[dict]) -> dict[str, str]:
    """From the online, env-resolved devices, bake one
    ``MESHTASTIC_MCP_ENV_<ROLE>=<env>`` override per board.

    The role key is each device's *bench role* — the board on its pinned hub slot
    (``tests/_bench.py``), which is the only thing that tells the three same-VID
    0x239a nRF52 boards apart. Keying by the coarse VID ``role`` column instead
    collapses all three onto ``nrf52``, and last-writer-wins then flashes the
    wrong firmware onto two of them. A device that doesn't sit on a known bench
    slot falls back to its coarse VID role (so non-bench setups still work). Rows
    without an env are skipped (so native/TCP nodes never become a flash
    target)."""
    bench = _load_bench()
    overrides: dict[str, str] = {}
    for row in rows:
        env = row.get("env")
        if not env:
            continue
        role_key = None
        if bench is not None:
            role_key = bench.role_for_hub_slot(row.get("hub_location"), row.get("hub_port"))
        if not role_key:
            role_key = row.get("role")
        if not role_key:
            continue
        overrides[f"MESHTASTIC_MCP_ENV_{role_key.upper()}"] = env
    return overrides


def tier_for(nodeid: str) -> str:
    """Derive a tier from a pytest nodeid path."""
    path = nodeid.split("::", 1)[0]
    parts = path.split("/")
    if "tests" in parts:
        rest = parts[parts.index("tests") + 1 :]
        if rest:
            seg = rest[0]
            if seg.endswith(".py"):
                return "bake" if "bake" in seg else "unit"
            return seg
    return "unit"


def _split_nodeid(nodeid: str) -> tuple[str, str]:
    path, _, name = nodeid.partition("::")
    return path, name or nodeid


def _nodeid_param(nodeid: str) -> str | None:
    """The parametrize id from a nodeid (``...::test_bake[rak4631]`` ->
    ``rak4631``), or None when the test isn't parametrized."""
    if not nodeid or not nodeid.endswith("]") or "[" not in nodeid:
        return None
    return nodeid[nodeid.rindex("[") + 1 : -1] or None


class TestRunner:
    """Owns the live pytest subprocess + its reportlog tail. One per app."""

    def __init__(self, db, hub, serialmon=None) -> None:
        self.db = db
        self.hub = hub
        self.serialmon = serialmon
        self._task: asyncio.Task | None = None

    async def start(self, args: list[str]) -> dict:
        from . import firmware  # local import to avoid a cycle at module load

        if is_running():
            raise RuntimeError("a test run is already in progress")

        fw = firmware.firmware_ref()
        from ..db import repo_devices as rd

        overrides = resolve_env_overrides(await rd.online_with_env(self.db))

        from ..db import repo_runs as rr

        run_id = await rr.create_run(
            self.db,
            args=args,
            seed=str(int(time.time())),
            fw_branch=fw.get("branch"),
            fw_sha=fw.get("sha"),
            fw_dirty=bool(fw.get("dirty")),
        )
        _state.update(running=True, run_id=run_id, exit_code=None)
        # Free every device port before the pytest subprocess grabs them: close
        # all serial monitors (UI + the Datadog fleet-log capture). is_running()
        # is already True, so the forwarder won't re-acquire mid-run.
        if self.serialmon is not None:
            # Bound the serial teardown: freeing the monitor ports must never be
            # able to wedge a run *before pytest spawns* (a stuck reader or a
            # saturated executor must not hold the whole harness hostage).
            try:
                await asyncio.wait_for(self.serialmon.suspend_all(), timeout=10.0)
            except TimeoutError:
                log.warning("suspend_all timed out — launching pytest anyway")
        await self.hub.publish("test.progress", {"type": "run_started", "run_id": run_id})
        self._task = asyncio.create_task(self._drive(run_id, args, overrides))
        return status()

    async def stop(self) -> None:
        proc = _state.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def reset(self) -> None:
        """Force-clear a wedged run without restarting the server. Cancels the
        drive task (its finally restores serial monitors + the run row) and
        hard-resets the run flag as a fallback if no task is reachable."""
        proc = _state.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        _state.update(running=False, proc=None)
        if self.serialmon is not None:
            try:
                await self.serialmon.resume_all()
            except Exception:
                pass

    async def _drive(self, run_id: int, args: list[str], overrides: dict) -> None:

        from ..db import repo_runs as rr

        exit_code = None
        report = Path(tempfile.gettempdir()) / f"fleetsuite-report-{run_id}.jsonl"
        report.unlink(missing_ok=True)
        # EVERYTHING that can fail lives inside this try so the finally below
        # always clears the run flag. (A stray exception in the setup — e.g. a
        # bad import — used to escape before the try and wedge the run at
        # running:true forever with no pytest and no way to recover.)
        try:
            # pytest runs from the repo root (where tests/ lives); the bench
            # tiers locate a firmware checkout via MESHTASTIC_FIRMWARE_ROOT.
            try:
                root = _repo_root()
            except Exception:
                root = Path.cwd()
            env = dict(os.environ)
            env.update(overrides)
            cmd, cwd, env_extra = build_pytest_invocation(report, args, root)
            env.update(env_extra)

            # Bound the spawn itself: launching the subprocess must never be able
            # to park forever (if create_subprocess_exec wedges, the whole run
            # would hang at running:true with no child and no progress).
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=(str(cwd) if cwd else None),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=SPAWN_TIMEOUT,
            )
            _state["proc"] = proc
            tail = asyncio.create_task(self._tail_report(run_id, report))
            watchdog = asyncio.create_task(self._startup_watchdog(proc, report))
            heartbeat = asyncio.create_task(self._heartbeat())
            await asyncio.gather(
                self._pump(proc.stdout, "stdout"),
                self._pump(proc.stderr, "stderr"),
            )
            exit_code = await proc.wait()
            await asyncio.sleep(0.2)  # let the reportlog flush
            watchdog.cancel()
            tail.cancel()
            heartbeat.cancel()
        except TimeoutError:
            await self.hub.publish(
                "test.stdout",
                {
                    "line": (
                        f"— pytest did not start within {int(SPAWN_TIMEOUT)}s; the "
                        "subprocess launch wedged (see /api/debug/tasks) —"
                    ),
                    "source": "stderr",
                },
            )
            exit_code = 124
        except FileNotFoundError as exc:
            await self.hub.publish(
                "test.stdout", {"line": f"failed to launch pytest: {exc}", "source": "stderr"}
            )
            exit_code = 127
        except Exception as exc:
            log.exception("test run %s failed to start", run_id)
            await self.hub.publish(
                "test.stdout",
                {"line": f"test run failed to start: {exc!r}", "source": "stderr"},
            )
            exit_code = 1
        finally:
            _state.update(
                running=False,
                exit_code=exit_code,
                proc=None,
                nodeid=None,
                since=None,
                last_line=None,
            )
            await rr.finish_run(self.db, run_id, exit_code=exit_code)
            # Run is over (is_running() now False) — restore serial monitors;
            # the next discovery scan re-establishes the fleet-log capture.
            if self.serialmon is not None:
                await self.serialmon.resume_all()
            await self.hub.publish(
                "test.progress", {"type": "run_finished", "exit_code": exit_code}
            )
            report.unlink(missing_ok=True)

    async def _startup_watchdog(self, proc, report: Path) -> None:
        """Fail a wedged launch instead of hanging forever. If pytest never
        starts — no reportlog written and the process still alive after
        STARTUP_SILENCE_TIMEOUT — terminate it. A normal pytest writes the
        reportlog within seconds, which lets this return early; this only fires
        for a launch that produced nothing at all (the macOS fork-from-Cocoa
        failure mode build_pytest_invocation works around)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_SILENCE_TIMEOUT
        while loop.time() < deadline:
            if proc.returncode is not None or report.exists():
                return  # launched (or already finished) — nothing to guard
            await asyncio.sleep(1.0)
        if proc.returncode is not None or report.exists():
            return
        await self.hub.publish(
            "test.stdout",
            {
                "line": (
                    f"— pytest produced nothing in {int(STARTUP_SILENCE_TIMEOUT)}s; "
                    "terminating a wedged launch —"
                ),
                "source": "stderr",
            },
        )
        for action in ("terminate", "kill"):
            try:
                getattr(proc, action)()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
                return
            except TimeoutError:
                continue

    async def _pump(self, stream, source: str) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\n")
            if line.strip():
                _state["last_line"] = line  # newest activity, for the heartbeat
            await self.hub.publish("test.stdout", {"line": line, "source": source})

    async def _heartbeat(self) -> None:
        """While a test is in flight, emit a periodic liveness frame (current
        test + elapsed + last output line) so the UI shows activity even during
        a single long test (a bake compile/flash) that streams little output."""
        while is_running():
            await asyncio.sleep(HEARTBEAT_S)
            if not is_running() or not _state.get("nodeid"):
                continue
            await self.hub.publish(
                "test.progress",
                {
                    "type": "heartbeat",
                    "nodeid": _state["nodeid"],
                    "elapsed_s": _elapsed_s(),
                    "last_line": _state.get("last_line"),
                },
            )

    async def _tail_report(self, run_id: int, report: Path) -> None:
        """Follow the reportlog JSONL and translate entries into progress frames
        + persisted results."""
        from ..db import repo_runs as rr

        seen_register: set[str] = set()
        pos = 0
        while True:
            if report.exists():
                with open(report, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                for raw in chunk.split(b"\n"):
                    if not raw.strip():
                        continue
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    # One malformed entry must never kill the tail task — that
                    # would silently stop ALL result/progress recording mid-run
                    # (the tier counters + run totals would freeze at 0/0/0).
                    try:
                        await self._handle_entry(run_id, entry, seen_register, rr)
                    except Exception:
                        log.exception("test_runner: bad report entry, skipping")
            await asyncio.sleep(0.3)

    async def _register(self, nodeid: str, seen_register: set) -> None:
        """Publish a 'register' frame so the UI creates a leaf for this test
        (which the tier counters + tree count). Idempotent via seen_register."""
        seen_register.add(nodeid)
        path, name = _split_nodeid(nodeid)
        await self.hub.publish(
            "test.progress",
            {
                "type": "register",
                "nodeid": nodeid,
                "tier": tier_for(nodeid),
                "file": path,
                "testname": name,
            },
        )

    async def _handle_entry(self, run_id, entry, seen_register, rr) -> None:
        rtype = entry.get("$report_type")
        if rtype == "CollectReport":
            # NOTE: `result` is None for directory/module collectors (the key is
            # present but null) — `.get("result", [])` would still return None,
            # so `or []` is essential or the whole tail task crashes on the first
            # collect report. In practice these don't carry test nodeids anyway
            # (registration happens from TestReport setup below); kept as a
            # belt-and-suspenders for pytest versions that DO populate it.
            for item in entry.get("result") or []:
                nodeid = item.get("nodeid") if isinstance(item, dict) else None
                if not nodeid or "::" not in nodeid or nodeid in seen_register:
                    continue
                await self._register(nodeid, seen_register)
        elif rtype == "TestReport":
            nodeid = entry.get("nodeid")
            when = entry.get("when")
            outcome = entry.get("outcome")
            if when == "setup":
                # Register the test here — the reliable source of nodeids, since
                # CollectReport.result is empty in this reportlog version. Without
                # this, no leaf exists and the tier counters stay 0/0/0.
                if nodeid and "::" in nodeid and nodeid not in seen_register:
                    await self._register(nodeid, seen_register)
                # Mark this the in-flight test so the heartbeat reports it.
                _state["nodeid"] = nodeid
                _state["since"] = time.time()
                _state["last_line"] = None
                await self.hub.publish("test.progress", {"type": "running", "nodeid": nodeid})
            # Final outcome: the call phase normally, or a non-passed setup
            # (skip/error) that short-circuits the test.
            final = when == "call" or (when == "setup" and outcome != "passed")
            if final:
                duration = entry.get("duration")
                await self.hub.publish(
                    "test.progress",
                    {
                        "type": "outcome",
                        "nodeid": nodeid,
                        "outcome": outcome,
                        "duration": duration,
                    },
                )
                longrepr = entry.get("longrepr")
                await rr.add_result(
                    self.db,
                    run_id,
                    nodeid=nodeid,
                    tier=tier_for(nodeid or ""),
                    outcome=outcome,
                    duration_s=duration,
                    device_serial=None,
                    longrepr=str(longrepr) if longrepr else None,
                )
                # A passing session bake just (re)provisioned the board on that
                # bench slot — stamp it so the UI can tell baked boards from ones
                # that are merely online.
                if outcome == "passed":
                    await self._record_bake_flashed(run_id, nodeid, rr)

    async def _record_bake_flashed(self, run_id, nodeid, rr) -> None:
        """When ``test_bake[<role>]`` passes, mark the board on that bench slot
        as provisioned-by-the-suite (``flashed_fw_branch/sha/at``) — the signal
        the device card uses to flag an online-but-never-baked board.

        The board is found by the bench role's *hub slot*, not its serial, so it
        works even for the three identical-VID nRF52 boards. Best-effort: any
        failure (no bench registry, slot unbound, db absent) just skips the
        stamp — it must never break result recording."""
        if self.db is None or not nodeid or tier_for(nodeid) != "bake":
            return
        bench = _load_bench()
        if bench is None:
            return
        role = _nodeid_param(nodeid)
        hub_port = bench.location_hub_port(bench.role_location(role)) if role else None
        if not hub_port:
            return
        from ..db import repo_devices as rd

        dev = await rd.by_hub_slot(self.db, location=hub_port[0], port=hub_port[1])
        if dev is None:
            return
        run = await rr.get_run(self.db, run_id) if hasattr(rr, "get_run") else None
        await rd.record_flashed(
            self.db,
            dev["serial_number"],
            branch=(run or {}).get("fw_branch"),
            sha=(run or {}).get("fw_sha"),
        )
        await self.hub.publish("device.update", await rd.get(self.db, dev["serial_number"]))
