# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Optional: run a self-contained local llama.cpp server (the Gemma 4 one-liner).

Lets the offload backend be a single ``llama-server`` binary instead of the Ollama
daemon. Gemma 4 has day-0 image+text support in llama.cpp, so the same offload and
vision tools work against it unchanged:

    llama serve -hf ggml-org/gemma-4-E2B-it-GGUF --port 8080   # OpenAI-compatible /v1

Point the client at it with ``MESHTASTIC_MCP_LOCAL_BACKEND=openai`` (base URL
defaults to ``http://127.0.0.1:8080/v1``). This module finds the binary, optionally
installs it (opt-in, networked), and starts/stops/queries a detached server whose
pid is tracked in a small state file so it survives across tool calls.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "ggml-org/gemma-4-E2B-it-GGUF"
DEFAULT_PORT = 8080
INSTALL_URL = "https://llama.app/install.sh"


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "meshtastic-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d / "llama-server.json"


def binary() -> str | None:
    """Path to the ``llama`` multitool or a standalone ``llama-server``, if present."""
    return shutil.which("llama") or shutil.which("llama-server")


def available() -> bool:
    return binary() is not None


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _reachable(port: int, *, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def install(*, timeout: float = 300.0) -> dict:
    """Run the official llama.cpp installer (``curl | sh``). Networked + mutating."""
    if binary():
        return {"ok": True, "already": True, "binary": binary()}
    proc = subprocess.run(
        f"curl -LsSf {INSTALL_URL} | sh",
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0 and binary() is not None,
        "binary": binary(),
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


def serve(model_ref: str = DEFAULT_MODEL, *, port: int = DEFAULT_PORT, wait: float = 180.0) -> dict:
    """Start ``llama serve -hf <model_ref>`` detached; block until it answers ``/v1``.

    Idempotent: returns the existing server if one is already up. The child runs in
    its own session (``start_new_session``) so it outlives the calling tool.
    """
    bin_ = binary()
    if bin_ is None:
        raise RuntimeError("llama binary not found; call install() or install llama.cpp first")

    st = _read_state()
    if st.get("pid") and _pid_alive(int(st["pid"])) and _reachable(int(st.get("port", port))):
        p = int(st.get("port", port))
        return {"ok": True, "already": True, "url": f"http://127.0.0.1:{p}/v1", **st}

    if Path(bin_).name == "llama":
        cmd = [bin_, "serve", "-hf", model_ref, "--port", str(port), "--host", "127.0.0.1"]
    else:  # standalone llama-server
        cmd = [bin_, "-hf", model_ref, "--port", str(port), "--host", "127.0.0.1"]

    log_path = _state_path().with_suffix(".log")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    state = {"pid": proc.pid, "port": port, "model": model_ref, "binary": bin_}
    _state_path().write_text(json.dumps(state))

    deadline = time.time() + wait
    while time.time() < deadline:
        if not _pid_alive(proc.pid):
            return {
                "ok": False,
                "error": "llama-server exited early",
                "log": str(log_path),
                **state,
            }
        if _reachable(port):
            return {"ok": True, "url": f"http://127.0.0.1:{port}/v1", "log": str(log_path), **state}
        time.sleep(2)
    return {
        "ok": False,
        "error": "timed out waiting for llama-server",
        "log": str(log_path),
        **state,
    }


def stop() -> dict:
    """Terminate the tracked server (whole process group) and clear state."""
    st = _read_state()
    pid = st.get("pid")
    if not pid:
        return {"ok": True, "stopped": False, "reason": "no tracked server"}
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    _state_path().unlink(missing_ok=True)
    return {"ok": True, "stopped": True, "pid": pid}


def status() -> dict:
    st = _read_state()
    port = int(st.get("port", DEFAULT_PORT))
    return {
        "binary": binary(),
        "pid": st.get("pid"),
        "running": bool(st.get("pid") and _pid_alive(int(st["pid"]))),
        "reachable": _reachable(port),
        "model": st.get("model"),
        "url": f"http://127.0.0.1:{port}/v1" if st else None,
    }
