# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Bridge to the Meshtastic Kotlin SDK sample CLI (headless JVM device IO).

Optional capability. Shells out to the ``cli`` launcher built from
``meshtastic-sdk``'s ``samples/cli`` (Gradle ``application`` plugin →
``installDist``), drives it in ``--json`` (NDJSON) mode, and parses the
envelope stream. This lets the MCP use the Kotlin SDK's engine (BLE / TCP /
USB-serial, two-stage handshake, NodeDB, ACK correlation) as an alternative
device backend to the Python ``meshtastic`` library — exactly the pattern the
MCP already uses to shell out to ``pio`` / ``adb`` / ``idb`` / ``esptool``.

This is a thin, untrusted bridge: a separate JVM process owns the radio link;
we only build argv, run it, and parse stdout. Nothing here mutates a device
without an explicit caller request (only ``send_text`` transmits).

Wire contract (``samples/cli`` ``Output.kt``): NDJSON, one
``{"type","ts","data"}`` object per line. Stable ``type`` values: ``info``,
``node``, ``packet``, ``event``, ``state``, ``scan-hit``, ``probe-run``,
``probe-summary``, ``error``, ``done``. The stream terminates with a ``done``
(``data:{reason,exit}``) or ``error`` (``data:{code,message}``) envelope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Env knobs ------------------------------------------------------------------
CLI_ENV = "MESHTASTIC_MCP_SDK_CLI"  # explicit path to the `cli` launcher script
ROOT_ENV = "MESHTASTIC_SDK_ROOT"  # a meshtastic-sdk checkout (we derive installDist)

# Path under a meshtastic-sdk checkout where `:samples:cli:installDist` lands.
_INSTALL_REL = Path("samples/cli/build/install/cli/bin")


class SdkCliError(RuntimeError):
    """Raised when the SDK CLI is missing or returns an unusable result."""


def cli_path() -> str | None:
    """Resolve the SDK ``cli`` launcher, or ``None`` if not installed.

    Resolution order:
      1. ``$MESHTASTIC_MCP_SDK_CLI`` — explicit path to the launcher script.
      2. ``$MESHTASTIC_SDK_ROOT`` + ``samples/cli/build/install/cli/bin/cli``.
      3. ``cli`` on ``PATH`` (only if it looks like the launcher dir).
    """
    explicit = os.environ.get(CLI_ENV)
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.is_file() else None

    root = os.environ.get(ROOT_ENV)
    if root:
        base = Path(root).expanduser() / _INSTALL_REL
        for name in ("cli", "cli.bat"):
            cand = base / name
            if cand.is_file():
                return str(cand)

    found = shutil.which("cli")
    return found if found else None


def available() -> bool:
    """True when the SDK CLI launcher is resolvable (no JVM is spawned here)."""
    return cli_path() is not None


# Transport syntax -----------------------------------------------------------
def normalize_transport(transport: str) -> str:
    """Map a caller-friendly endpoint to the CLI's ``--transport`` syntax.

    Accepts the SDK syntax verbatim (``tcp:host[:port]`` / ``serial:port[:baud]``
    / ``ble:needle``) and also translates the MCP/`meshtastic`-style forms:
      - ``tcp://host[:port]``      → ``tcp:host[:port]``
      - ``/dev/ttyUSB0`` etc.      → ``serial:/dev/ttyUSB0``
    """
    t = transport.strip()
    if t.startswith("tcp://"):
        return "tcp:" + t[len("tcp://") :]
    if t.startswith(("tcp:", "serial:", "ble:")):
        return t
    # A bare path / COM port → serial.
    if t.startswith(("/", "COM", "cu.", "tty")):
        return "serial:" + t
    raise SdkCliError(
        f"Unrecognized transport {transport!r}; use tcp:host[:port], "
        "serial:port[:baud], ble:needle, tcp://host, or a serial device path."
    )


# Envelope parsing -----------------------------------------------------------
def parse_envelopes(stdout: str) -> list[dict[str, Any]]:
    """Parse NDJSON stdout into a list of envelope dicts (skips junk lines)."""
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            out.append(obj)
    return out


def _summarize(returncode: int, envelopes: list[dict[str, Any]], stderr: str) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in envelopes:
        by_type.setdefault(str(e.get("type")), []).append(e.get("data", {}))
    err_env = next((e for e in envelopes if e.get("type") == "error"), None)
    done_env = next((e for e in reversed(envelopes) if e.get("type") == "done"), None)
    exit_code = returncode
    if done_env and isinstance(done_env.get("data"), dict):
        exit_code = int(done_env["data"].get("exit", returncode))
    ok = returncode == 0 and err_env is None
    return {
        "ok": ok,
        "exit": exit_code,
        "by_type": by_type,
        "envelopes": envelopes,
        "error": (err_env or {}).get("data") if err_env else None,
        "stderr": stderr.strip() or None,
    }


# Invocation -----------------------------------------------------------------
def run(
    subcmd: list[str],
    transport: str,
    *,
    timeout_ms: int = 15000,
    run_timeout: float = 90.0,
) -> dict[str, Any]:
    """Run ``cli --json <subcmd> --transport <spec> --timeout <ms>`` and parse.

    ``subcmd`` is the subcommand and its own flags (e.g. ``["info"]`` or
    ``["send", "text", "-m", "hi"]``). ``transport`` is normalized to the CLI
    syntax. Returns the parsed summary dict; never raises on a device-level
    failure (it surfaces as ``ok=False`` + ``error``), only on a missing CLI or
    a hard process timeout.
    """
    launcher = cli_path()
    if launcher is None:
        raise SdkCliError(
            f"SDK CLI not found. Set {CLI_ENV} to the `cli` launcher or "
            f"{ROOT_ENV} to a meshtastic-sdk checkout with `installDist` built."
        )
    spec = normalize_transport(transport)
    argv = [launcher, "--json", *subcmd, "--transport", spec, "--timeout", str(int(timeout_ms))]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=run_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SdkCliError(
            f"SDK CLI timed out after {run_timeout}s running {' '.join(subcmd)!r}"
        ) from exc
    envelopes = parse_envelopes(proc.stdout)
    return _summarize(proc.returncode, envelopes, proc.stderr)


# High-level helpers ---------------------------------------------------------
def device_info(transport: str, *, timeout_ms: int = 15000) -> dict[str, Any]:
    """One-shot ``info``: own node + node count (the ``info`` envelope's data)."""
    res = run(["info"], transport, timeout_ms=timeout_ms)
    info = (res["by_type"].get("info") or [{}])[0]
    res["info"] = info
    return res


def list_nodes(transport: str, *, timeout_ms: int = 15000) -> dict[str, Any]:
    """Snapshot the node DB; returns each ``node`` envelope's data under ``nodes``."""
    res = run(["nodes"], transport, timeout_ms=timeout_ms)
    res["nodes"] = res["by_type"].get("node") or []
    return res


def send_text(
    transport: str,
    message: str,
    *,
    to: str = "BROADCAST",
    channel: int = 0,
    await_ms: int = 30000,
    timeout_ms: int = 15000,
) -> dict[str, Any]:
    """Transmit a text message and await Acked/Delivered/Failed (device-mutating)."""
    subcmd = [
        "send",
        "text",
        "-m",
        message,
        "--to",
        to,
        "--channel",
        str(int(channel)),
        "--await",
        f"{int(await_ms)}ms",
    ]
    return run(subcmd, transport, timeout_ms=timeout_ms)


def status() -> dict[str, Any]:
    """Report whether the SDK-CLI backend is available and where it resolves."""
    path = cli_path()
    return {
        "available": path is not None,
        "cli_path": path,
        "env": {CLI_ENV: os.environ.get(CLI_ENV), ROOT_ENV: os.environ.get(ROOT_ENV)},
    }
