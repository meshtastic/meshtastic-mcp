# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Out-of-process camera capture worker.

OpenCV's macOS backend (AVFoundation / Core Media I/O) can **segfault the host
process** when a capture device's video format changes underneath it — e.g. the
USB churn when the test harness power-cycles a hub mid-run. That crash is a
native SIGSEGV, uncatchable from Python, and it took down the whole FleetSuite
server.

So ALL OpenCV access lives here, in a short-lived child process. If cv2 crashes,
only this worker dies; the server reads EOF on the pipe and carries on (it
respawns the worker for a live stream). Invoked as::

    python -m meshtastic_mcp.web.camera_worker <mode> <arg>

Modes:
  stream <index>      write framed JPEGs to stdout forever (binary)
  probe  <index>      write one JSON object {ok,width,height,error}; exit
  probe-many <csv>    write one JSON object {cv2,results:{idx:{...}}}; exit
                      (an empty <csv> just reports cv2 availability)

Stream framing: each frame is ``MAGIC`` + uint32 big-endian length + JPEG bytes,
so the binary stream is unambiguous over the pipe.
"""

from __future__ import annotations

import json
import struct
import sys
import time

# Frame marker for the streaming protocol. camera_stream (the reader) imports
# this so the two stay in lock-step.
MAGIC = b"MJF1"
FPS = 10.0


def _load_cv2():
    import cv2  # type: ignore

    try:  # silence the noisy "can't open camera" probe warnings
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass
    return cv2


def _probe(cv2, index: int) -> dict:
    """Open a capture index briefly to confirm it works + read its resolution."""
    try:
        cap = cv2.VideoCapture(index)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        if not cap.isOpened():
            return {"ok": False, "error": "device did not open (in use or absent?)"}
        ok, _ = cap.read()
        if not ok:
            return {"ok": False, "error": "no frame from device"}
        return {
            "ok": True,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        }
    finally:
        cap.release()


def _stream(cv2, index: int) -> int:
    out = sys.stdout.buffer
    try:
        cap = cv2.VideoCapture(index)
    except Exception:
        return 3
    if not cap.isOpened():
        return 3
    period = 1.0 / FPS
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                return 0  # device went away — clean end, parent will respawn
            enc_ok, buf = cv2.imencode(".jpg", frame)
            if enc_ok:
                jpg = buf.tobytes()
                try:
                    out.write(MAGIC + struct.pack(">I", len(jpg)) + jpg)
                    out.flush()
                except (BrokenPipeError, OSError):
                    return 0  # parent closed the pipe (client disconnected)
            time.sleep(period)
    finally:
        cap.release()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: camera_worker <stream|probe|probe-many> <arg>\n")
        return 2
    mode, arg = argv[1], argv[2]
    try:
        cv2 = _load_cv2()
    except Exception as exc:
        if mode in ("probe", "probe-many"):
            sys.stdout.write(json.dumps({"cv2": False, "error": f"opencv unavailable: {exc}"}))
        return 4
    if mode == "stream":
        return _stream(cv2, int(arg))
    if mode == "probe":
        sys.stdout.write(json.dumps(_probe(cv2, int(arg))))
        return 0
    if mode == "probe-many":
        idxs = []
        for x in arg.split(","):
            s = x.strip()
            if not s:
                continue
            try:
                idxs.append(int(s))
            except ValueError:
                continue  # skip junk rather than crashing the worker
        results = {str(i): _probe(cv2, i) for i in idxs}
        sys.stdout.write(json.dumps({"cv2": True, "results": results}))
        return 0
    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
