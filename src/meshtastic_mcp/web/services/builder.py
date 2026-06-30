"""Firmware build orchestrator.

Builds are keyed by ``(env, fw_sha)`` and cached on disk under
``$MESHTASTIC_MCP_ARTIFACT_DIR/<sha>/<env>``. ``enqueue`` returns immediately
with a row per env (``status: queued`` for a fresh build, ``cached: True`` when
an artifact already exists); the actual compile runs in a background task so the
HTTP request never blocks. The compile itself is injectable (``build_fn``) so
the queue/cache/dedup logic is testable without Docker or a real toolchain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from ..db import repo_builds as rb

log = logging.getLogger("meshtastic_mcp.web.builder")

# Signature of an injectable build function: compile ``env`` into ``out``,
# streaming progress through ``log_cb``; return True on success.
BuildFn = Callable[[str, Path, Callable[[str], None]], bool]


def _artifact_root() -> Path:
    return Path(
        os.environ.get(
            "MESHTASTIC_MCP_ARTIFACT_DIR",
            str(Path.home() / ".meshtastic_mcp" / "artifacts"),
        )
    )


def artifact_dir(sha: str, env: str) -> Path:
    return _artifact_root() / sha / env


def cached_artifact_dir(sha: str, env: str) -> Path | None:
    """The cached artifact dir for a key if a successful build left one, else
    None. 'Successful' is proven by the presence of a flashable image."""
    d = artifact_dir(sha, env)
    if d.is_dir() and (
        any(d.glob("*.factory.bin")) or any(d.glob("*.bin")) or any(d.glob("*.uf2"))
    ):
        return d
    return None


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        import subprocess

        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


# pio compile output is thousands of lines; only forward the ones that read
# as progress so the build queue shows a meaningful "current step" without
# flooding the WS with every -I flag.
_PROGRESS_PREFIXES = (
    "Compiling",
    "Linking",
    "Building",
    "Archiving",
    "Indexing",
    "Generating",
    "Checking",
    "Processing",
    "RAM:",
    "Flash:",
    "Took",
    "SUCCESS",
    "FAILED",
)


def _is_progress(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return s.startswith(_PROGRESS_PREFIXES) or "error:" in s.lower()


def default_build_fn(env: str, out: Path, log_cb: Callable[[str], None]) -> bool:
    """Host pio build, copying the resulting images into ``out``. Best-effort —
    used when no build_fn is injected. Streams progress lines through
    ``log_cb`` as the compile runs so the queue shows live feedback."""
    try:
        from meshtastic_mcp import config as mcfg
        from meshtastic_mcp import pio

        root = mcfg.firmware_root()
        log_cb(f"pio run -e {env}")

        def on_line(line: str) -> None:
            if _is_progress(line):
                log_cb(line.strip())

        result = pio.run(
            ["run", "-e", env],
            cwd=root,
            timeout=900,
            check=False,
            line_cb=on_line,
        )
        if result.returncode != 0:
            log_cb(pio.tail_lines(result.stdout + result.stderr, 40))
            return False
        out.mkdir(parents=True, exist_ok=True)
        build_dir = root / ".pio" / "build" / env
        copied = 0
        for pattern in ("*.factory.bin", "*.bin", "*.uf2", "*.hex"):
            for f in build_dir.glob(pattern):
                shutil.copy2(f, out / f.name)
                copied += 1
        log_cb(f"copied {copied} artifact(s)")
        return copied > 0
    except Exception as exc:
        log_cb(f"build error: {exc}")
        return False


class BuildOrchestrator:
    def __init__(self, db, hub, build_fn: BuildFn | None = None) -> None:
        self.db = db
        self.hub = hub
        self.build_fn = build_fn or default_build_fn
        self._inflight: dict[str, asyncio.Task] = {}

    async def enqueue(
        self, envs: list[str], *, sha: str, branch: str | None, force: bool = False
    ) -> list[dict]:
        """Queue a build per env (skipping ones already cached or in flight).
        ``force`` rebuilds even when an artifact is cached. Returns a status row
        per requested env."""
        results: list[dict] = []
        for env in envs:
            key = f"{env}@{sha}"

            cached = None if force else cached_artifact_dir(sha, env)
            if cached is not None:
                row = await rb.get(self.db, env, sha)
                if row is None or row["status"] not in ("success", "cached"):
                    bid = await rb.create(
                        self.db, env=env, fw_sha=sha, fw_branch=branch, status="cached"
                    )
                    row = await rb.set_status(
                        self.db,
                        bid,
                        status="cached",
                        duration_s=0.0,
                        artifact_dir=str(cached),
                    )
                results.append({**row, "cached": True})
                await self.hub.publish("build.update", {**row, "cached": True})
                continue

            if key in self._inflight and not self._inflight[key].done():
                row = await rb.get(self.db, env, sha)
                if row:
                    results.append(row)
                continue

            bid = await rb.create(self.db, env=env, fw_sha=sha, fw_branch=branch, status="queued")
            row = await rb.get_by_id(self.db, bid)
            results.append(row)
            await self.hub.publish("build.update", row)
            self._inflight[key] = asyncio.create_task(self._run_build(bid, env, sha, key))
        return results

    async def _run_build(self, build_id: int, env: str, sha: str, key: str) -> None:
        loop = asyncio.get_running_loop()
        row = await rb.set_status(self.db, build_id, status="building")
        await self.hub.publish("build.update", row)

        out = artifact_dir(sha, env)
        logs: list[str] = []

        def log_cb(line: str) -> None:
            logs.append(line)
            # Logs go on their OWN topic — publishing them as build.update would
            # overwrite the row (status/sha/duration) with a partial object and
            # hide the build's progress.
            self.hub.publish_threadsafe("build.log", {"id": build_id, "env": env, "line": line})

        start = loop.time()
        try:
            ok = await asyncio.to_thread(self.build_fn, env, out, log_cb)
        except Exception as exc:
            logs.append(f"exception: {exc}")
            ok = False
        duration = round(loop.time() - start, 2)

        row = await rb.set_status(
            self.db,
            build_id,
            status="success" if ok else "failed",
            duration_s=duration,
            artifact_dir=str(out) if ok else None,
            error=None if ok else "\n".join(logs[-20:]) or "build failed",
        )
        await self.hub.publish("build.update", row)
        self._inflight.pop(key, None)
