# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""MJPEG camera streaming — capture runs out-of-process.

All OpenCV access happens in a child process (``meshtastic_mcp.web.camera_worker``)
because cv2's macOS backend can SIGSEGV on video-format races (USB churn during a
test run). Isolating it means such a crash kills only the worker, never the
FleetSuite server: a live stream just respawns the worker, and the one-shot
probe/discover calls return "unavailable" instead of taking the process down.

Name enumeration (``system_profiler`` / sysfs) needs no OpenCV, so it stays
in-process and works even without the ``[ui]`` extra installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from ..camera_worker import MAGIC

log = logging.getLogger("meshtastic_mcp.web.camera_stream")

BOUNDARY = "frame"
_MAX_INDEX = 10  # don't probe past this
_PROBE_TIMEOUT = 20.0
_FRAME_MAX = 64 * 1024 * 1024  # sanity cap on a single JPEG frame
_PIPE_LIMIT = 8 * 1024 * 1024  # StreamReader buffer — JPEG frames exceed the 64K default
_MAX_EMPTY_RESPAWNS = 3  # stop respawning a worker that never yields a frame


def _worker_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "meshtastic_mcp.web.camera_worker", *args]


def _worker_json(args: list[str], timeout: float = _PROBE_TIMEOUT) -> dict | None:
    """Run a one-shot worker mode that prints one JSON object and parse it. A
    worker crash (including a cv2 segfault), timeout, or missing-opencv all
    collapse to None — the server is never affected."""
    try:
        proc = subprocess.run(_worker_cmd(*args), capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("camera worker %s failed: %s", args, exc)
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        return None


def _enumerate_names() -> list[tuple[int, str]]:
    """Best-effort camera names from the OS, WITHOUT activating any device (no
    OpenCV). Returns ``[(index, name), ...]``."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["system_profiler", "SPCameraDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            items = json.loads(out.stdout).get("SPCameraDataType", [])
            return [(i, it.get("_name", f"camera {i}")) for i, it in enumerate(items)]
        except Exception:
            return []
    if sys.platform.startswith("linux"):
        out: list[tuple[int, str]] = []
        for node in sorted(Path("/sys/class/video4linux").glob("video*")):
            try:
                idx = int(node.name[len("video") :])
                name = (node / "name").read_text().strip()
                out.append((idx, name or f"video{idx}"))
            except Exception:
                continue
        return out
    return []


def discover(skip: set[str] | None = None, probe_resolution: bool = True) -> dict:
    """Enumerate attached cameras. Resolutions (and cv2 availability) come from a
    single out-of-process worker call, so probing a flaky camera can't crash us.
    ``skip`` is the set of indices already bound to a FleetSuite stream — marked
    ``in_use`` and not re-opened."""
    skip = skip or set()
    named = _enumerate_names()

    # One worker call probes the relevant indices and reports cv2 availability.
    if probe_resolution:
        if named:
            probe_idxs = [i for i, _ in named if i < _MAX_INDEX and str(i) not in skip]
        else:
            probe_idxs = list(range(_MAX_INDEX))  # no OS names → discover via cv2
    else:
        probe_idxs = []
    info = _worker_json(["probe-many", ",".join(str(i) for i in probe_idxs)])
    cv2_ok = bool(info and info.get("cv2"))
    results: dict = (info or {}).get("results", {})

    # If the OS gave us no names but cv2 is here, fall back to index labels.
    if not named and cv2_ok:
        named = [(i, f"camera {i}") for i in range(_MAX_INDEX)]

    cameras: list[dict] = []
    for index, name in named:
        if index >= _MAX_INDEX:
            continue
        entry: dict = {"index": index, "name": name, "in_use": str(index) in skip}
        if cv2_ok and not entry["in_use"] and probe_resolution:
            r = results.get(str(index))
            if r and r.get("ok"):
                entry["width"] = r.get("width", 0)
                entry["height"] = r.get("height", 0)
            else:
                # Named by the OS but cv2 couldn't open it — surface, don't drop.
                entry["unavailable"] = True
        cameras.append(entry)

    return {"available": True, "cv2": cv2_ok, "cameras": cameras}


def snapshot(device_index: str, *, rotation: int = 0, mirror: bool = False) -> bytes | None:
    """One still JPEG from a capture index, out-of-process (same crash
    isolation as the stream). Blocking — call via ``asyncio.to_thread``.
    Returns None on any failure."""
    try:
        idx = int(device_index)
    except (ValueError, TypeError):
        return None
    try:
        proc = subprocess.run(
            _worker_cmd("still", str(idx), str(rotation), "1" if mirror else "0"),
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("camera still %s failed: %s", device_index, exc)
        return None
    out = proc.stdout
    header = len(MAGIC) + 4
    if len(out) < header or not out.startswith(MAGIC):
        return None
    (size,) = struct.unpack(">I", out[len(MAGIC) : header])
    payload = out[header : header + size]
    return payload if len(payload) == size and size > 0 else None


def probe(device_index: str) -> dict:
    """Can we open this device? Returns ``{ok, error}``. Runs out-of-process."""
    try:
        idx = int(device_index)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"invalid device index: {device_index!r}"}
    info = _worker_json(["probe", str(idx)])
    if info is None:
        return {"ok": False, "error": "camera worker failed (opencv missing or crashed)"}
    return {"ok": bool(info.get("ok")), "error": info.get("error")}


async def _read_frames(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield JPEG payloads from a worker's framed stdout until EOF/desync."""
    while True:
        try:
            magic = await stream.readexactly(len(MAGIC))
            if magic != MAGIC:
                return  # desync — bail rather than emit garbage
            (size,) = struct.unpack(">I", await stream.readexactly(4))
            if size <= 0 or size > _FRAME_MAX:
                return
            payload = await stream.readexactly(size)
        except Exception:
            # Worker exited (clean EOF or crash) or the pipe errored — any read
            # failure just ends the stream; mjpeg() decides whether to respawn.
            return
        yield payload


async def _terminate(proc) -> None:
    # Tear the worker down without ever raising out of the caller's finally
    # (which would crash the generator instead of closing it cleanly).
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    except Exception:
        pass


async def mjpeg(device_index: str) -> AsyncIterator[bytes]:
    """Async MJPEG frame generator backed by the out-of-process capture worker.
    If the worker dies (incl. a cv2 segfault) the stream respawns it, so a
    transient camera crash blips the feed instead of killing the server. When
    the client disconnects, the generator closes and the worker is torn down."""
    try:
        idx = str(int(device_index))
    except (ValueError, TypeError):
        return

    empty_respawns = 0
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_worker_cmd("stream", idx),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_PIPE_LIMIT,
            )
        except OSError:
            return
        assert proc.stdout is not None  # stdout=PIPE above
        produced = False
        try:
            async for jpg in _read_frames(proc.stdout):
                produced = True
                empty_respawns = 0
                yield (
                    b"--" + BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                )
        finally:
            # Runs on worker death AND on client disconnect (GeneratorExit). On
            # disconnect the exception propagates past the loop, so we don't
            # respawn; on death we fall through and respawn below.
            await _terminate(proc)

        if not produced:
            empty_respawns += 1
            if empty_respawns >= _MAX_EMPTY_RESPAWNS:
                return  # camera is gone — stop hot-looping
        await asyncio.sleep(0.5)
