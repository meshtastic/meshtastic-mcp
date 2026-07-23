# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FastMCP server wiring — device discovery, admin, recorder, and tooling.

Each tool handler is a thin delegation to a named module (pio.py, admin.py, etc.); business
logic does not live here. Firmware-coupled tools register only when the firmware capability is
active (see ``firmware_tool`` / ``_FIRMWARE_TOOLS``); modern MCP hint metadata is applied to
every tool post-registration (see ``_apply_tool_annotations``).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import (
    admin,
    boards,
    capabilities,
    connection,
    devices,
    fixtures,
    flash,
    hw_tools,
    info,
    log_query,
    registry,
    rf_oracle,
    serial_session,
)
from . import (
    config_snapshot as config_snapshot_mod,
)
from . import (
    doctor as doctor_mod,
)
from . import (
    inject as inject_mod,
)
from . import (
    pa_sweep as pa_sweep_mod,
)
from . import sdk_cli as sdk_cli_mod
from . import userprefs as userprefs_mod
from .recorder import get_recorder
from .replay import ReplayParams
from .replay import build as replay_build
from .replay import capture as replay_capture
from .replay import engine as replay_engine
from .replay import fuzz as replay_fuzz
from .replay import get_manager as get_replay_manager
from .replay import sim as replay_sim

log = logging.getLogger(__name__)

app = FastMCP("meshtastic-mcp")
# Report our package version in the MCP handshake; otherwise the SDK falls back to
# advertising its own version (`pkg_version("mcp")`) as the server version.
from . import __version__ as _pkg_version  # noqa: E402

app._mcp_server.version = _pkg_version

# Capability detection drives conditional tool registration. The portable core always
# registers; firmware-coupled tools (build/flash/boards/userprefs) register only when a
# firmware checkout is present, so a `pip install meshtastic-mcp` with no firmware tree
# still exposes the full device/admin/recorder surface instead of advertising tools that
# would error on every call.
CAPS = capabilities.detect()


def firmware_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the firmware capability is active.

    Without a firmware tree the handler is still defined (so imports/tests are unaffected)
    but is not advertised to MCP clients.
    """

    def deco(fn):
        if CAPS.firmware:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def android_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the android capability is active.

    Without the `android` CLI + `adb` the Android tools are not advertised so
    they cannot be called and cannot error on a missing binary.
    """

    def deco(fn):
        if CAPS.android:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def apple_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the apple capability is active.

    Without `xcrun` (+ optional `idb`) the Apple tools are not advertised.
    """

    def deco(fn):
        if CAPS.apple:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def local_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when a local Ollama model is reachable.

    Gates the optional offload tools (summarize/triage recorder windows) so they
    aren't advertised — and can't error — when no local model is available.
    """

    def deco(fn):
        if CAPS.local_model:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def local_serve_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but registers when a local backend is reachable *or* a
    `llama`/`llama-server` binary is present — so the bootstrap/status tools show
    up even before any model is up (their whole job is to bring one up).
    """

    def deco(fn):
        if CAPS.local_model or CAPS.llama_server:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def sdr_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the sdr capability is active.

    Without `pyrtlsdr` + an attached RTL-SDR, the RF-compliance oracle tools
    (`rf_scan`, `rf_confirm_tx`) are not advertised. See `doctor` for what's missing.
    """

    def deco(fn):
        if CAPS.sdr:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def sdk_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the Kotlin SDK CLI is present.

    Gates the experimental device-IO bridge tools that shell out to the
    meshtastic-sdk headless JVM CLI (an alternative to the Python `meshtastic`
    library); plain installs without the CLI are unaffected.
    """

    def deco(fn):
        if CAPS.sdk_cli:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def _start_recorder() -> None:
    # Persistent device-log capture. Starts on first import — pubsub fan-out
    # is process-global, so subscribing here captures every active interface
    # (whether opened by an MCP tool, a pytest fixture, or a serial_session).
    # Files land in mcp-server/.mtlog/ (gitignored). See recorder/recorder.py
    # for the full design. Recorder startup is best-effort: an unwritable
    # log dir or pubsub mismatch should not take the MCP server down.
    try:
        get_recorder().start()
    except Exception as exc:
        log.warning("Failed to start persistent recorder: %s", exc)


_start_recorder()


# Android-coupled tools, gated by `android_tool` on the android capability.
_ANDROID_TOOLS = (
    "android_docs_search",
    "android_docs_fetch",
    "android_version_lookup",
    "android_render_compose_preview",
)

# SDR-coupled tools, gated by `sdr_tool` on the sdr capability.
_SDR_TOOLS = (
    "rf_scan",
    "rf_confirm_tx",
)

# Firmware-coupled tools, gated by `firmware_tool` on the firmware capability.
_FIRMWARE_TOOLS = (
    "list_boards",
    "get_board",
    "build_start",
    "build_poll",
    "build",
    "clean",
    "pio_flash",
    "flash_start",
    "flash_poll",
    "erase_and_flash",
    "update_flash",
    "userprefs_manifest",
    "userprefs_get",
    "userprefs_set",
    "userprefs_reset",
    "userprefs_testing_profile",
    "push_fake_nodedb",
)


def _log_capabilities() -> None:
    try:
        log.info("meshtastic-mcp capabilities active: %s", CAPS.summary())
        if not CAPS.android:
            log.info(
                "android capability inactive: %d android tools not registered: %s",
                len(_ANDROID_TOOLS),
                ", ".join(_ANDROID_TOOLS),
            )
        if not CAPS.firmware:
            log.info(
                "firmware capability inactive: %d build/flash tools not registered "
                "(set MESHTASTIC_FIRMWARE_ROOT to enable): %s",
                len(_FIRMWARE_TOOLS),
                ", ".join(_FIRMWARE_TOOLS),
            )
        if not CAPS.sdr:
            log.info(
                "sdr capability inactive: %d RF-compliance tools not registered "
                "(install the 'sdr' extra + librtlsdr + an RTL-SDR to enable): %s",
                len(_SDR_TOOLS),
                ", ".join(_SDR_TOOLS),
            )
    except Exception as exc:  # never fail startup over a capability probe
        log.warning("capability detection failed: %s", exc)


_log_capabilities()


# ---------- Discovery & metadata ------------------------------------------


@app.tool()
def doctor() -> dict[str, Any]:
    """Probe external dependencies and report how to acquire any that are missing.

    Returns a structured environment report: active capability groups, per-dependency
    status (ok/missing/degraded), what each is needed for, and the **exact, current,
    platform-aware command** to install anything missing (`fix_commands`). Call this
    first when an android/apple/firmware tool fails with a missing-prerequisite error,
    or to self-provision the environment before a hardware-free e2e run.
    """
    return doctor_mod.run().to_dict()


@android_tool()
def android_docs_search(query: str) -> str:
    """Search the official Android Knowledge Base for grounded API/Compose/UI guidance.

    Use this instead of guessing when debugging the Meshtastic-Android app, authoring a
    UI journey, or answering an Android API question. Returns ranked articles with
    `kb://` URLs; pass one to `android_docs_fetch` for the full text. Requires the
    `android` CLI (see `doctor`).
    """
    from .emulator import avd

    return avd.docs_search(query)


@android_tool()
def android_docs_fetch(url: str) -> str:
    """Fetch the full text of an Android Knowledge Base article by its `kb://...` URL."""
    from .emulator import avd

    return avd.docs_fetch(url)


@android_tool()
def android_version_lookup(query: str) -> str:
    """Look up the latest version of a maven artifact / Android SDK / tool (`android studio`).

    e.g. "androidx.compose.material3:material3" or "agp". Use for dependency-freshness checks.
    Requires the `android` CLI (see `doctor`).
    """
    from .emulator import avd

    return avd.version_lookup(query)


@android_tool()
def android_render_compose_preview(file: str, preview: str | None = None) -> str:
    """Render a Jetpack Compose @Preview to a PNG without an emulator (fast UI regression).

    `file` is a Kotlin source path; `preview` optionally selects one @Preview function.
    Needs a running Android Studio (the `android` CLI drives it). Returns the output PNG path.
    """
    from .emulator import avd

    return avd.render_compose_preview(file, preview=preview)


# ---------------------------------------------------------------------------
# Output format helpers
# ---------------------------------------------------------------------------


def _to_markdown_table(rows: list[dict[str, Any]]) -> str:
    """Render a list of flat dicts as a GitHub-flavoured Markdown table.

    Saves 40–60% tokens vs JSON on large result sets by emitting field names
    once in the header row instead of on every row.
    """
    if not rows:
        return "(no results)"
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


@app.tool()
def triage_bundle(start: str = "-2m", end: str = "now") -> dict[str, Any]:
    """Collect the device-plane artifacts for an e2e-failure window in one call.

    Returns `{packets, logs, events}` from the recorder for the time window (defaults to the
    last 2 minutes). Pairs with the `triage_e2e_failure` prompt and the `triage.md` skill
    reference: align these timestamps with the app `layout`/`screen capture` at the deadline
    to classify a failure (never-sent / sent-not-received / received-not-rendered /
    rendered-not-asserted). `start`/`end` accept `-5m`, `now`, or epoch/ISO.
    """
    return {
        "window": {"start": start, "end": end},
        "packets": log_query.packets_window(start, end),
        "logs": log_query.logs_window(start, end),
        "events": log_query.events_window(start, end),
    }


@local_tool()
def summarize_window(
    start: str = "-5m",
    end: str = "now",
    focus: str = "anomalies and errors",
    lane: str = "default",
) -> dict[str, Any]:
    """Distill a recorder window into a short summary via a **local** model (offload).

    Pushes token-heavy log/packet triage onto a local Ollama GPU instead of the
    agent's context: pulls the window's logs + packets + events and asks the
    local model for a concise summary focused on `focus` (e.g. "errors",
    "reboots", "who talked to whom"). `lane` picks the model (`fast`/`default`).

    Returns `{summary, model, counts}`. Treat the summary as an untrusted draft —
    verify against the raw windows before acting on anything correctness-critical.
    Only registered when a local model is reachable (`get_active_capabilities`).
    """
    import json

    from . import local_model

    logs = log_query.logs_window(start, end, max_lines=200)
    packets = log_query.packets_window(start, end, max=200)
    events = log_query.events_window(start, end, max=100)
    blob = json.dumps(
        {
            "logs": logs.get("lines", logs),
            "packets": packets.get("packets", packets),
            "events": events.get("events", events),
        },
        default=str,
    )[:24000]
    system = (
        "You are a Meshtastic device-log triage assistant. Given recorder JSON "
        "(firmware logs, mesh packets, connection/node events), produce a terse "
        "bullet summary. Be specific (node ids, portnums, error strings, counts). "
        "No preamble."
    )
    prompt = f"Focus on: {focus}\n\nRecorder window {start}..{end}:\n{blob}"
    try:
        summary = local_model.complete(prompt, system=system, lane=lane, num_predict=400)
    except local_model.LocalModelError as exc:
        return {"error": str(exc)}
    return {
        "summary": summary,
        "model": local_model.model(lane),
        "counts": {
            "logs": len(logs.get("lines", [])),
            "packets": len(packets.get("packets", [])),
            "events": len(events.get("events", [])),
        },
    }


@local_tool()
def vision_oracle(image_path: str, question: str) -> dict[str, Any]:
    """Assert about a screenshot with a **local** vision model (offline oracle).

    The `vision-oracle.md` fallback, run on-box: when the a11y tree is empty
    (WebView / Canvas / map / mid-animation), capture a screenshot and ask a
    yes/no question — e.g. `vision_oracle("/tmp/shot.png", "Does a message bubble
    containing 'E2E-1782' appear?")`. Screenshots never leave the host.

    Returns `{match: bool, answer, evidence, model}`. A draft oracle — prefer the
    exact-match a11y tree when it works. Needs a multimodal local model
    (`MESHTASTIC_MCP_LOCAL_VISION`). Registered only when a local model is up.
    """
    from . import local_model

    try:
        return local_model.vision_assert(image_path, question)
    except (local_model.LocalModelError, OSError) as exc:
        return {"error": str(exc)}


@local_tool()
def triage_window(
    start: str = "-2m", end: str = "now", token: str = "", screenshot: str = ""
) -> dict[str, Any]:
    """First-pass e2e-failure triage via a **local** model (offload the correlation).

    Pulls the device-plane window (logs+packets+events) and asks the local model
    to propose the failure bucket — `never_sent` / `sent_not_received` /
    `received_not_rendered` / `rendered_not_asserted` — with the one-line
    evidence, mirroring `triage.md`. Pass the marker `token` to scope it and a
    `screenshot` path to fold a local **vision** read of the app side into the call.

    Returns `{bucket, reasoning, vision, counts}`. **The agent owns the final
    PASS/FAIL verdict** — this is a draft hypothesis to confirm against the raw
    artifacts. Registered only when a local model is reachable.
    """
    import json

    from . import local_model

    bundle = triage_bundle(start, end)
    vision = None
    if screenshot:
        q = (
            f"Does the app screen show the message/marker '{token}'?"
            if token
            else ("Describe what app screen and key content is visible.")
        )
        try:
            vision = local_model.vision_assert(screenshot, q)
        except (local_model.LocalModelError, OSError) as exc:
            vision = {"error": str(exc)}
    blob = json.dumps(bundle, default=str)[:20000]
    system = (
        "You triage Meshtastic device<->app e2e failures. Given the device-plane "
        "recorder window (and optionally an app-side vision read), classify the "
        "failure into exactly one bucket and justify in one line. Buckets: "
        "never_sent, sent_not_received, received_not_rendered, rendered_not_asserted, "
        "no_failure. Reply:\nBUCKET: <one>\nWHY: <one line>."
    )
    prompt = (
        f"marker token: {token or '(none)'}\n"
        f"app vision read: {json.dumps(vision) if vision else '(none)'}\n\n"
        f"device window {start}..{end}:\n{blob}"
    )
    try:
        raw = local_model.complete(prompt, system=system, lane="default", num_predict=160)
    except local_model.LocalModelError as exc:
        return {"error": str(exc)}
    bucket, why = "unclear", raw.strip()
    for line in raw.splitlines():
        low = line.strip().lower()
        if low.startswith("bucket:"):
            bucket = line.split(":", 1)[1].strip()
        elif low.startswith("why:"):
            why = line.split(":", 1)[1].strip()
    return {
        "bucket": bucket,
        "reasoning": why,
        "vision": vision,
        "counts": {
            "packets": len(bundle["packets"].get("packets", [])),
            "logs": len(bundle["logs"].get("lines", [])),
            "events": len(bundle["events"].get("events", [])),
        },
    }


@local_serve_tool()
def local_model_status() -> dict[str, Any]:
    """Report the local-model backend: which one, reachability, and models.

    Shows the active backend (``ollama`` or ``openai``), its URL, whether it
    answers, the model lanes in use, and — if a llama.cpp server is managed here —
    its pid/url. Registered when a backend is reachable or a `llama` binary exists.
    """
    from . import llama_server, local_model

    return {
        "backend": local_model.backend(),
        "url": (
            local_model.base_url() if local_model.backend() == "openai" else local_model.host()
        ),
        "reachable": local_model.available(),
        "models": local_model.list_models(),
        "lanes": {lane: local_model.model(lane) for lane in ("default", "fast", "vision")},
        "llama_server": llama_server.status(),
    }


@local_serve_tool()
def local_model_serve(
    model_ref: str = "ggml-org/gemma-4-E2B-it-GGUF", port: int = 8080, install: bool = False
) -> dict[str, Any]:
    """Start a self-contained llama.cpp server (the Gemma 4 one-liner) as the backend.

    Runs ``llama serve -hf <model_ref> --port <port>`` detached and waits until its
    OpenAI-compatible ``/v1`` answers, then points this process's client at it
    (sets backend=openai + base URL). Pass ``install=True`` to fetch the llama.cpp
    binary first (opt-in, networked). Self-contained alternative to the Ollama daemon.

    Note: offload tools (summarize/vision/triage) are gated at startup, so to
    *register* them against a fresh llama-server, set the env and start the server
    before launching the MCP server. Once running, ``local_model.*`` calls route here.
    """
    import os

    from . import llama_server

    if install and not llama_server.available():
        ins = llama_server.install()
        if not ins.get("ok"):
            return {"ok": False, "stage": "install", **ins}
    try:
        result = llama_server.serve(model_ref, port=port)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    if result.get("ok"):
        os.environ["MESHTASTIC_MCP_LOCAL_BACKEND"] = "openai"
        os.environ["MESHTASTIC_MCP_LOCAL_BASE_URL"] = result["url"]
    return result


@local_serve_tool()
def local_model_serve_stop() -> dict[str, Any]:
    """Stop the llama.cpp server started by ``local_model_serve`` (if any)."""
    from . import llama_server

    return llama_server.stop()


@sdk_tool()
def sdk_status() -> dict[str, Any]:
    """Report the Kotlin-SDK device-IO bridge: whether the CLI launcher resolves.

    Experimental alternative device backend that shells out to the meshtastic-sdk
    headless JVM CLI (BLE/TCP/serial engine) instead of the Python `meshtastic`
    library. Set ``MESHTASTIC_MCP_SDK_CLI`` (or ``MESHTASTIC_SDK_ROOT``).
    """
    return sdk_cli_mod.status()


@sdk_tool()
def sdk_device_info(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """One-shot device snapshot via the Kotlin SDK CLI (own node + node count).

    ``transport`` accepts ``tcp:host[:port]`` / ``serial:port[:baud]`` /
    ``ble:needle`` (or ``tcp://host`` / a serial device path). Returns the parsed
    ``info`` envelope plus the full envelope stream; ``ok=False`` with ``error``
    on a device-level failure. The Kotlin engine owns the link in a JVM subprocess.
    """
    return sdk_cli_mod.device_info(transport, timeout_ms=timeout_ms)


@sdk_tool()
def sdk_list_nodes(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """Snapshot the device node DB via the Kotlin SDK CLI (read-only).

    Returns each node's wire-JSON under ``nodes`` (from the CLI's ``node``
    envelopes). ``transport`` syntax as in ``sdk_device_info``.
    """
    return sdk_cli_mod.list_nodes(transport, timeout_ms=timeout_ms)


@sdk_tool()
def sdk_send_text(
    transport: str,
    message: str,
    to: str = "BROADCAST",
    channel: int = 0,
    await_ms: int = 30000,
    timeout_ms: int = 15000,
) -> dict[str, Any]:
    """Transmit a text message via the Kotlin SDK CLI and await the outcome.

    Device-mutating: injects a mesh packet. ``to`` is ``BROADCAST``, a decimal
    node num, or ``0xHEX``. Waits up to ``await_ms`` for Acked/Delivered/Failed.
    """
    return sdk_cli_mod.send_text(
        transport, message, to=to, channel=channel, await_ms=await_ms, timeout_ms=timeout_ms
    )


@app.tool()
def list_devices(
    include_unknown: bool = False,
    format: str = "json",
) -> list[dict[str, Any]] | str:
    """List USB/serial ports, flagging those likely to be Meshtastic devices.

    With include_unknown=True, returns every serial port the OS knows about
    (useful for debugging when a device isn't detected). Otherwise returns
    only likely-Meshtastic candidates.

    format: "json" (default) returns a list of dicts; "table" returns a
    Markdown table — use "table" for large results or human-readable display
    (saves ~40-60% tokens vs JSON).

    Returns (json):
        [{port, vid, pid, description, manufacturer, likely_meshtastic, ...}]
    """
    result = devices.list_devices(include_unknown=include_unknown)
    return _to_markdown_table(result) if format == "table" else result


@firmware_tool()
def list_boards(
    architecture: str | None = None,
    actively_supported_only: bool = False,
    query: str | None = None,
    board_level: str | None = None,
    format: str = "json",
) -> list[dict[str, Any]] | str:
    """Enumerate PlatformIO envs (boards) with Meshtastic metadata.

    architecture: filter to one arch ("esp32", "esp32s3", "nrf52840", "rp2040", "stm32", "native").
    actively_supported_only: filter to boards marked custom_meshtastic_actively_supported=true.
    query: substring match on display_name, env name, or hw_model_slug (case-insensitive).
    board_level: "release" (default-track release boards), "pr" (PR CI), or "extra" (opt-in extras).
    format: "json" (default) or "table" for a compact Markdown summary.

    Returns (json):
        [{env, architecture, hw_model, hw_model_slug, display_name,
          actively_supported, support_level, board_level, tags, images}]
    """
    result = boards.list_boards(
        architecture=architecture,
        actively_supported_only=actively_supported_only,
        query=query,
        board_level=board_level,
    )
    if format == "table":
        summary = [
            {
                "env": b.get("env", ""),
                "arch": b.get("architecture", ""),
                "display_name": b.get("display_name", ""),
                "supported": b.get("actively_supported", ""),
            }
            for b in result
        ]
        return _to_markdown_table(summary)
    return result


@firmware_tool()
def get_board(env: str) -> dict[str, Any]:
    """Full metadata for one PlatformIO env, including raw pio config fields."""
    return boards.get_board(env)


# ---------- Build & flash -------------------------------------------------


@firmware_tool()
def build_start(
    env: str,
    with_manifest: bool = True,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a firmware build in the background and return immediately.

    Returns a `build_id` within ~1 second. Poll with `build_poll(build_id)`
    to check progress and retrieve output. Use this instead of `build` when
    your MCP client has a short per-call timeout (the synchronous `build`
    blocks for 2–5 minutes which exceeds most client timeouts).
    """
    return flash.build_start(
        env,
        with_manifest=with_manifest,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )


@firmware_tool()
def build_poll(build_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Poll the status of a background build started with `build_start`.

    Returns status (running/done/failed), elapsed_s, artifacts, and the last
    `tail_lines` lines of build output. Call every few seconds until status
    is 'done' or 'failed'.
    """
    return flash.build_poll(build_id, tail_lines=tail_lines)


@firmware_tool()
def build(
    env: str,
    with_manifest: bool = True,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build firmware for one env via `pio run -e <env>`.

    Returns exit_code, duration, artifact paths under .pio/build/<env>/, and
    tails of stdout/stderr (last 200 lines each). with_manifest=True adds the
    mtjson target which produces an .mt.json manifest alongside the firmware.

    `userprefs` (optional): dict of `USERPREFS_<KEY>: value` baked into this
    build via userPrefs.jsonc injection. The file is restored after the build
    completes. Use `userprefs_manifest` to discover available keys. Use
    `userprefs_set` for persistent changes.

    `build_flags` (optional): dict of `-D<NAME>=<VALUE>` macros for this build
    only, injected via `PLATFORMIO_BUILD_FLAGS`. Common pattern:
    `build_flags={"DEBUG_HEAP": 1}` enables per-thread leak detection + a
    `[heap N]` prefix on every log line. The recorder picks the prefix up
    automatically and synthesizes a high-resolution heap timeline that
    `telemetry_timeline(field="free_heap")` can read alongside the normal
    ~60 s LocalStats packets. Pair with `/leakhunt` for classification.
    """
    return flash.build(
        env,
        with_manifest=with_manifest,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )


@firmware_tool()
def clean(env: str) -> dict[str, Any]:
    """Clean one env's build output via `pio run -e <env> -t clean`.

    Useful when switching branches or debugging a stale-cache build failure.
    """
    return flash.clean(env)


@firmware_tool()
def pio_flash(
    env: str,
    port: str,
    confirm: bool = False,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flash firmware via `pio run -e <env> -t upload --upload-port <port>`.

    Works for any architecture (ESP32/nRF52/RP2040/STM32). Requires confirm=True.
    For first-time flashing a blank ESP32 board (erase + bootloader + app + fs),
    prefer `erase_and_flash`. For ESP32 OTA updates, prefer `update_flash`.

    `userprefs` (optional): dict of `USERPREFS_<KEY>: value` baked into this
    build via userPrefs.jsonc injection; restored after upload.

    `build_flags` (optional): dict of `-D<NAME>=<VALUE>` macros for the
    rebuild-before-upload, e.g. `{"DEBUG_HEAP": 1}`. Required for the flags
    to actually land in the uploaded firmware — without it, the implicit
    rebuild relinks without the env var and silently drops them.
    """
    return flash.flash(
        env,
        port,
        confirm=confirm,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )


@firmware_tool()
def flash_start(
    env: str,
    port: str,
    confirm: bool = False,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a firmware flash in the background and return immediately.

    Returns a `job_id` within ~1 second. Poll with `flash_poll(job_id)` until
    status is 'done' or 'failed'. Use this instead of `pio_flash` when your MCP
    client has a short per-call timeout — the upload step routinely exceeds the
    60 s default. Requires confirm=True.

    Returns:
        {job_id: str, status: "running", log_path: str}
    """
    return flash.flash_start(
        env,
        port,
        confirm=confirm,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )


@firmware_tool()
def flash_poll(job_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Poll the status of a background flash started with `flash_start`.

    Returns status (running/done/failed), elapsed_s, exit_code, and the last
    `tail_lines` lines of upload output. Call every few seconds until terminal.
    """
    return flash.flash_poll(job_id, tail_lines=tail_lines)


@firmware_tool()
def erase_and_flash(
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32-only: full erase + factory flash via bin/device-install.sh.

    Wipes the entire flash and writes bootloader, app, OTA, and LittleFS
    partitions from the factory.bin. Requires confirm=True. Runs `build` first
    if no factory.bin is present (set skip_build=True to require a prior build).

    `userprefs` (optional): dict of `USERPREFS_<KEY>: value` baked into the
    factory.bin via userPrefs.jsonc injection. When provided, forces a rebuild
    (skip_build=True is incompatible). File is restored after upload.
    """
    return flash.erase_and_flash(
        env, port, confirm=confirm, skip_build=skip_build, userprefs_overrides=userprefs
    )


@firmware_tool()
def update_flash(
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32-only: OTA app-partition update via bin/device-update.sh.

    Updates only the application partition, preserving device config and node
    database. Faster than erase_and_flash but won't recover a broken bootloader.
    Requires confirm=True. Builds first if needed.

    `userprefs` (optional): dict of `USERPREFS_<KEY>: value` baked into the
    firmware.bin via userPrefs.jsonc injection. When provided, forces a rebuild.
    """
    return flash.update_flash(
        env, port, confirm=confirm, skip_build=skip_build, userprefs_overrides=userprefs
    )


# ---------- USERPREFS discovery & persistence -----------------------------


@firmware_tool()
def userprefs_manifest() -> dict[str, Any]:
    """Full manifest of USERPREFS_* keys the firmware knows about.

    Combines `userPrefs.jsonc` (active + commented examples) with a scan of
    `src/**` for `USERPREFS_<KEY>` references — so every key the firmware
    actually consumes shows up, even if undocumented in the jsonc.

    Each entry has: key, active (is it uncommented), value (current), example
    (jsonc commented default), declared_in_jsonc, consumed_by (list of src
    files), inferred_type (brace|number|bool|enum|string|unknown).

    `inferred_type` mirrors how platformio-custom.py wraps values at build
    time: `brace` = byte array `{ 0x01, ... }`, `number` = decimal, `bool` =
    true/false, `enum` = `meshtastic_*` constant, `string` = wrapped in quotes
    via StringifyMacro.
    """
    return userprefs_mod.build_manifest()


@firmware_tool()
def userprefs_get() -> dict[str, Any]:
    """Return the current userPrefs.jsonc state.

    `active` is the dict of uncommented `USERPREFS_*` → value that will be
    baked into the next build. `commented` is the dict of commented example
    defaults (shown for reference).
    """
    state = userprefs_mod.read_state()
    # Drop `order` (internal for round-trip rendering) from the public payload.
    return {
        "path": state["path"],
        "active": state["active"],
        "commented": state["commented"],
    }


@firmware_tool()
def userprefs_set(prefs: dict[str, Any]) -> dict[str, Any]:
    """Merge `prefs` into userPrefs.jsonc persistently (uncommenting keys).

    Existing active values not in `prefs` are kept. To remove a key from the
    active set, call `userprefs_reset` (restores the MCP backup if present)
    or edit the jsonc manually. Values are stringified the way
    platformio-custom.py expects (bool → "true"/"false", int → "42", etc.).
    """
    return userprefs_mod.merge_active(prefs)


@firmware_tool()
def userprefs_reset() -> dict[str, Any]:
    """Restore userPrefs.jsonc from the most recent MCP backup (if any).

    The backup is only created by the legacy `userprefs_set` workflow (not
    currently written automatically). Returns `{restored: bool, ...}` — false
    when no backup is present, in which case the caller should edit the
    jsonc directly.
    """
    return userprefs_mod.reset()


@firmware_tool()
def userprefs_testing_profile(
    psk_seed: str | None = None,
    channel_name: str = "McpTest",
    channel_num: int = 88,
    region: str = "US",
    modem_preset: str = "LONG_FAST",
    short_name: str | None = None,
    long_name: str | None = None,
    disable_mqtt: bool = True,
    disable_position: bool = False,
) -> dict[str, Any]:
    """Generate a USERPREFS dict for provisioning an isolated test-mesh device.

    Baking this into firmware produces devices that:
      - Run on a deterministic non-default LoRa slot (default 88 on US LONG_FAST,
        well off the `hash("LongFast")` slot a stock production device uses)
      - Join a private channel with a name and PSK that differ from public
        defaults — so no accidental mesh-with-production-devices
      - Have MQTT disabled (no uplink/downlink bridge), so test traffic never
        leaks to a public broker
      - Optionally disable GPS for bench-test conditions

    For a multi-device test cluster, pass the same `psk_seed` to every call so
    every device shares the same PSK and lands on the same isolated mesh.

    Returned dict is ready to pass straight to `build`, `pio_flash`,
    `erase_and_flash`, or `update_flash` via their `userprefs` parameter.

    Example:
        profile = userprefs_testing_profile(psk_seed="ci-run-2026-04-16")
        erase_and_flash(env="tbeam", port="/dev/cu.usbmodem...", confirm=True,
                        userprefs=profile)

    Args:
        psk_seed: seed for deterministic 32-byte PSK via SHA-256. None = random
            (fine one-off, useless for multi-device clusters).
        channel_name: primary channel name (≤11 chars). Default "McpTest".
        channel_num: 1-indexed LoRa slot (0 = fall back to name-hash). Default
            88 — mid-upper US band, unlikely to collide with production slots.
        region: short code — one of US, EU_433, EU_868, CN, JP, ANZ, KR, TW,
            RU, IN, NZ_865, TH, UA_433, UA_868, MY_433, MY_919, SG_923, LORA_24.
        modem_preset: one of LONG_FAST, LONG_SLOW, LONG_MODERATE, VERY_LONG_SLOW,
            MEDIUM_SLOW, MEDIUM_FAST, SHORT_SLOW, SHORT_FAST, SHORT_TURBO.
        short_name: optional owner short name (≤4 chars) stamped into the build.
        long_name: optional owner long name stamped into the build.
        disable_mqtt: disable MQTT module + uplink/downlink (default True).
        disable_position: disable GPS + smart-position broadcasts (default False).

    """
    return userprefs_mod.build_testing_profile(
        psk_seed=psk_seed,
        channel_name=channel_name,
        channel_num=channel_num,
        region=region,
        modem_preset=modem_preset,
        short_name=short_name,
        long_name=long_name,
        disable_mqtt=disable_mqtt,
        disable_position=disable_position,
    )


@app.tool()
def touch_1200bps(port: str, settle_ms: int = 250) -> dict[str, Any]:
    """Open `port` at 1200 baud and immediately close, triggering USB CDC
    bootloader entry on nRF52840, ESP32-S3 (native USB), RP2040, etc.

    After the touch, polls serial devices for up to 3 seconds and reports any
    new port that appeared (the bootloader often enumerates as a different
    device). Reboots the device into its bootloader (annotated destructive
    for that reason) but erases nothing — it is just a mode-switch signal.
    """
    return flash.touch_1200bps(port, settle_ms=settle_ms)


# ---------- Serial log sessions -------------------------------------------


@app.tool()
def serial_open(
    port: str,
    baud: int = 115200,
    env: str | None = None,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Open a `pio device monitor` session reading from `port`.

    If `env` is set, pio picks up monitor_speed and monitor_filters from
    platformio.ini — recommended for firmware debugging since it enables
    esp32_exception_decoder / esp32_c3_exception_decoder for ESP32 envs.

    Without `env`, uses the supplied baud and filters (default ["direct"]).
    Common filters: direct, time, hexlify, esp32_exception_decoder,
    esp32_c3_exception_decoder, log2file.

    Returns a session_id for use with serial_read / serial_close, plus the
    resolved baud and filters so callers can confirm what pio selected.
    """
    # Enforce the exclusive-port invariant atomically. The admin path
    # (connection.connect) already checks active_session_for_port + holds
    # port_lock; mirror that here so two concurrent serial_open calls (or a
    # serial_open racing an admin connect) can't both own `port`. The lock is
    # held only across the check + spawn + register; the running monitor's
    # ongoing ownership is then tracked via the registry session.
    lock = registry.port_lock(port)
    if not lock.acquire(blocking=False):
        raise connection.ConnectionError(
            f"Port {port} is busy — another device operation is in flight. Retry shortly."
        )
    try:
        active = registry.active_session_for_port(port)
        if active is not None:
            raise connection.ConnectionError(
                f"Port {port} is held by serial session {active.id}. Call `serial_close` first."
            )
        session = serial_session.open_session(port=port, baud=baud, env=env, filters=filters)
        registry.register_session(session)
    finally:
        lock.release()
    return {
        "session_id": session.id,
        "resolved_baud": session.baud,
        "resolved_filters": session.filters,
        "env": session.env,
    }


@app.tool()
def serial_read(
    session_id: str,
    max_lines: int = 200,
    since_cursor: int | None = None,
) -> dict[str, Any]:
    """Read buffered lines from a serial monitor session.

    Default: returns everything since your last call to serial_read (uses an
    advancing cursor). Pass `since_cursor=N` to re-read from a specific point,
    or `since_cursor=0` to read from the start of the in-memory buffer.

    Returns `dropped` = count of lines that aged out of the 10k-line ring
    buffer between reads — so a value > 0 means you missed data.
    """
    session = registry.get_session(session_id)
    return serial_session.read_session(session, max_lines=max_lines, since_cursor=since_cursor)


@app.tool()
def serial_list() -> list[dict[str, Any]]:
    """List all active serial monitor sessions."""
    return [serial_session.session_summary(s) for s in registry.all_sessions()]


@app.tool()
def serial_close(session_id: str) -> dict[str, Any]:
    """Terminate a serial monitor session and free its port.

    Returns:
        {ok: true} on success, or {ok: false, reason: str} if session unknown.
    """
    session = registry.remove_session(session_id)
    if session is None:
        return {"ok": False, "reason": f"Unknown session_id {session_id!r}"}
    serial_session.close_session(session)
    return {"ok": True}


# ---------- Device interaction: reads -------------------------------------


@app.tool()
def device_info(port: str | None = None, timeout_s: float = 8.0) -> dict[str, Any]:
    """Connect via meshtastic.SerialInterface and return a summary of the node.

    If `port` is omitted and exactly one likely-Meshtastic device is connected,
    it's auto-selected; otherwise the tool errors with the candidate list.
    """
    return info.device_info(port=port, timeout_s=timeout_s)


@app.tool()
def list_nodes(
    port: str | None = None,
    timeout_s: float = 8.0,
    format: str = "json",
) -> list[dict[str, Any]] | str:
    """Return the device's current node database (local node + all known peers).

    format: "json" (default) or "table" (Markdown). Use "table" when there
    are many nodes — repeating all field names per row in JSON costs ~7k
    tokens for 100 nodes; a table header appears once.

    Returns (json):
        [{node_num, user: {long_name, short_name, hw_model, role},
          position, snr, rssi, last_heard, battery_level, is_favorite}]
    """
    result = info.list_nodes(port=port, timeout_s=timeout_s)
    if format == "table":
        # Flatten nested user dict for tabular display
        flat = [
            {
                "node_num": n.get("node_num"),
                "short": (n.get("user") or {}).get("short_name", ""),
                "long_name": (n.get("user") or {}).get("long_name", ""),
                "hw_model": (n.get("user") or {}).get("hw_model", ""),
                "snr": n.get("snr", ""),
                "last_heard": n.get("last_heard", ""),
            }
            for n in result
        ]
        return _to_markdown_table(flat)
    return result


# ---------- Device interaction: writes ------------------------------------


@app.tool()
def set_owner(
    long_name: str, short_name: str | None = None, port: str | None = None
) -> dict[str, Any]:
    """Set the device's owner long name and (optional) short name (≤4 chars).

    Returns:
        {ok: true, long_name: str, short_name: str}
    """
    return admin.set_owner(long_name=long_name, short_name=short_name, port=port)


@app.tool()
def get_config(section: str | None = None, port: str | None = None) -> dict[str, Any]:
    """Read one or all config sections.

    `section` may be any LocalConfig section (device, position, power, network,
    display, lora, bluetooth, security) or LocalModuleConfig section (mqtt,
    serial, telemetry, external_notification, canned_message, range_test,
    store_forward, neighbor_info, ambient_lighting, detection_sensor,
    paxcounter, audio, remote_hardware, statusmessage, traffic_management).
    Omit or pass "all" for every section.
    """
    return admin.get_config(section=section, port=port)


@app.tool()
def set_config(path: str, value: Any, port: str | None = None) -> dict[str, Any]:
    """Set one config field via dot-path and write it to the device.

    Examples: "lora.region"="US", "lora.modem_preset"="LONG_FAST",
    "device.role"="ROUTER", "mqtt.enabled"=True, "mqtt.address"="host".
    Enum fields accept their name (case-insensitive) or int.

    Idempotent: calling with the same path+value twice leaves identical state.
    Call `reboot()` afterwards to commit the change to NVS.

    Returns:
        {ok: true, path: str, section: str, parent: str,
         old_value: any, new_value: any}
    """
    return admin.set_config(path=path, value=value, port=port)


@app.tool()
def get_channel_url(include_all: bool = False, port: str | None = None) -> dict[str, Any]:
    """Get the shareable channel URL (QR-code content).

    include_all=True returns the admin URL including all secondary channels;
    False returns only the primary channel (what users typically share).
    """
    return admin.get_channel_url(include_all=include_all, port=port)


@app.tool()
def set_channel_url(url: str, port: str | None = None) -> dict[str, Any]:
    """Import channels from a Meshtastic channel URL.

    Replaces the device's channel set with the channels encoded in `url`.
    Idempotent: applying the same URL twice leaves identical channel state.

    Returns:
        {ok: true, channels_imported: int}
    """
    return admin.set_channel_url(url=url, port=port)


@app.tool()
def set_debug_log_api(enabled: bool, port: str | None = None) -> dict[str, Any]:
    """Toggle security.debug_log_api_enabled on the local node.

    When true, firmware streams log lines as protobuf `LogRecord` messages
    over the StreamAPI (topic `meshtastic.log.line` in meshtastic-python)
    instead of raw text. Lets diagnostic clients capture firmware-side logs
    through the SAME SerialInterface used for admin/info calls — no
    separate `pio device monitor` session needed, no exclusive-port-lock
    conflict. Persists across reboot via NVS; wiped by factory_reset
    unless re-applied.

    The earlier emitLogRecord race (shared tx buffer) is fixed at the
    firmware level — the log path has a dedicated scratch + txBuf and
    both emission paths serialize via a mutex. Safe to leave on under
    traffic.
    """
    return admin.set_debug_log_api(enabled=enabled, port=port)


# ---------- Config snapshots + diff ---------------------------------------


@app.tool()
def config_snapshot(name: str, port: str | None = None) -> dict[str, Any]:
    """Capture the device's full config (localConfig + moduleConfig) to a named snapshot.

    Snapshots persist under the MCP data dir. Use before a firmware upgrade or
    config batch, then `config_diff` to see what changed. Overwrites an existing
    snapshot of the same name. `name` accepts [A-Za-z0-9._-] only.

    Returns:
        {ok: true, name: str, path: str, local_sections: [...], module_sections: [...]}
    """
    return config_snapshot_mod.capture(name, port=port)


@app.tool()
def config_snapshots_list() -> list[dict[str, Any]]:
    """List saved config snapshots with capture time and source port.

    Returns:
        [{name, captured_at (epoch), port, path}]
    """
    return config_snapshot_mod.list_snapshots()


@app.tool()
def config_diff(name_a: str, name_b: str | None = None, port: str | None = None) -> dict[str, Any]:
    """Diff two config snapshots, or one snapshot against the live device.

    If `name_b` is omitted, `name_a` is diffed against the current live device
    config (captured on the fly). Fields are keyed by dot-path, e.g.
    "localConfig.lora.region".

    Returns:
        {from: str, to: str, changed: {path: {from, to}}, added: {...},
         removed: {...}, identical: bool}
    """
    return config_snapshot_mod.diff(name_a, name_b, port=port)


def _confirm_tx(
    packet_id: int | None,
    port: str | None,
    tx_timeout_s: float,
) -> tuple[bool | None, float | None, str | None]:
    """Poll for evidence that `packet_id` actually reached the air.

    Returns `(confirmed, latency_s, reason)` where `confirmed` is:
      * `True`  — positive evidence of transmission,
      * `False` — an evidence channel was working and showed no transmission,
      * `None`  — no evidence channel available, so we genuinely cannot tell.

    The `None` case matters. A node cannot observe its own transmission through
    the receive path. The firmware *does* echo the packet back, but it omits the
    now-redundant `from` field on that echo, and
    `MeshInterface._handlePacketFromRadio` treats a missing `from` as "Device
    returned a packet we sent, ignoring" and returns before publishing any pubsub
    event (`mesh_interface.py`). So it never reaches `meshtastic.receive`, and so
    never reaches the recorder's packet stream. Reporting that absence as `False`
    claimed failure for messages that were verifiably delivered.

    `rf_oracle.confirm_tx` documents the same constraint from the RF side, and
    its `firmware_self_reported_tx` field carries the identical caveat — it is a
    known, documented limitation there, not a bug.

    Two things do constitute evidence:

    1. The firmware's own log line ``Started Tx (id=0x…)``, captured when
       `set_debug_log_api(True)` is on (or a `serial_session` is tapping the
       port). Deliberately NOT ``enqueue for send`` — a packet can be enqueued
       and then killed before airtime, which is exactly the bug PR #18 fixed,
       so matching the enqueue line would restore a false positive.
    2. A neighbour rebroadcasting our packet, which arrives back on the receive
       path carrying the same id. Real proof it reached the air, and it works
       even with no log capture.
    """
    if packet_id is None:
        return None, None, "no packet id was returned for this send, so nothing to match against"

    # Firmware prints the id as lowercase, zero-padded 32-bit hex. Push this at
    # `logs_window` as its `grep` rather than filtering the results ourselves:
    # grep is applied *before* the max_lines cap, so the one line we need can
    # never be truncated away by an unrelated burst of firmware chatter. That is
    # not hypothetical — an unfiltered 2 min window on real hardware measured
    # total_matched=455 / dropped=395.
    tx_pattern = rf"Started Tx \(id=0x{packet_id:08x}\b"
    t0 = time.monotonic()
    deadline = t0 + tx_timeout_s
    saw_any_log = False
    packets_truncated = False

    while True:
        hits = log_query.logs_window(start="-2m", port=port, grep=tx_pattern, max_lines=20)
        if hits.get("lines"):
            return True, round(time.monotonic() - t0, 2), None

        # Separate existence probe: "is any log flowing for this port at all?"
        # Only ever asks for one line, so the cap is irrelevant here too.
        if not saw_any_log:
            probe = log_query.logs_window(start="-2m", port=port, max_lines=1)
            if probe.get("lines") or probe.get("total_matched"):
                saw_any_log = True

        # A neighbour's rebroadcast comes back with our id. `packets_window` has
        # no grep, so honour `dropped` instead of silently trusting a capped list.
        packets = log_query.packets_window(start="-2m", max=200)
        for pkt in packets.get("packets", []):
            if pkt.get("id") == packet_id:
                return True, round(time.monotonic() - t0, 2), None
        if packets.get("dropped", 0) > 0:
            packets_truncated = True

        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)

    if not saw_any_log:
        reason = (
            "no firmware log lines were captured for this port, so transmission "
            "could not be observed — enable set_debug_log_api(True) (or hold a "
            "serial_session) to make tx_confirmed meaningful"
        )
        if packets_truncated:
            reason += (
                "; the packet window was also truncated (dropped>0), so a "
                "rebroadcast may have been missed — narrow the window and retry"
            )
        return None, None, reason
    return False, None, "firmware logs were captured but showed no 'Started Tx' for this packet"


@app.tool()
def send_text(
    text: str,
    to: str | int | None = None,
    channel_index: int = 0,
    want_ack: bool = False,
    port: str | None = None,
    wait_for_tx: bool = False,
    tx_timeout_s: float = 30.0,
    tx_linger_s: float = 8.0,
) -> dict[str, Any]:
    """Send a text message over the mesh.

    `to` defaults to broadcast ("^all"). Pass a node ID (hex string like
    "!abcdef01") or node number (int) to direct-message a specific node.
    channel_index picks which configured channel to send on.

    `tx_linger_s` delays the connection close after sendText() returns, allowing
    the firmware's channel-politeness TX delay (~4s) and RF airtime to complete
    before the serial port resets. Prevents loss of queued broadcasts.

    Delivery is async and best-effort. By default this returns as soon as the
    packet is queued. Set `wait_for_tx=True` to additionally poll (up to
    `tx_timeout_s`) for evidence the radio actually transmitted: the firmware's
    `Started Tx (id=…)` log line, or a neighbour rebroadcasting the packet.

    `tx_confirmed` is three-valued. `true` means transmission was observed;
    `false` means logs were flowing and showed no transmission; **`null` means
    it could not be observed at all** — most often because firmware logs are not
    being captured. Call `set_debug_log_api(True)` on the port (or hold a
    `serial_session`) to make confirmation meaningful; without it, `null` is the
    expected answer and does NOT mean the message failed. `tx_unconfirmed_reason`
    explains which case you got.

    Returns:
        {ok: true, packet_id: int | null, destination: str}
        plus when wait_for_tx=True:
        {tx_confirmed: bool | null, tx_latency_s: float | null,
         tx_unconfirmed_reason?: str}
    """
    result = admin.send_text(
        text=text,
        to=to,
        channel_index=channel_index,
        want_ack=want_ack,
        port=port,
        tx_linger_s=tx_linger_s,
    )
    if not wait_for_tx:
        return result

    confirmed, latency_s, reason = _confirm_tx(result.get("packet_id"), port, tx_timeout_s)
    result["tx_confirmed"] = confirmed
    result["tx_latency_s"] = latency_s
    if reason is not None:
        result["tx_unconfirmed_reason"] = reason
    return result


@app.tool()
def inject_frame(
    mode: str = "text",
    body: str | None = None,
    portnum: int | None = None,
    payload_hex: str = "",
    ciphertext_hex: str = "",
    long_name: str = "INJECTED",
    short_name: str = "INJ",
    session_hex: str = "",
    from_node: str = "0xdeadbeef",
    to: str | None = None,
    channel_index: int = 0,
    packet_id: str | None = None,
    want_response: bool = False,
    encrypt: bool = True,
    pki: bool = False,
    public_key_b64: str | None = None,
    fuzz_count: int = 10,
    fuzz_seed: int = 1,
    confirm: bool = False,
    port: str | None = None,
) -> dict[str, Any]:
    """Inject a packet into a connected board AS IF it arrived off the LoRa radio.

    Destructive/`confirm=True`-gated: it forges over-the-air traffic (incl. admin) into the target.

    The target must run firmware built with `-D MESHTASTIC_ENABLE_FRAME_INJECTION=1` (portduino
    sim nodes support it unconditionally). The frame enters the real receive pipeline, so it gets
    from!=0 enforcement, channel/PKC decryption, hop handling, dedup, and module dispatch — just
    like an over-the-air packet. Firmware seam: `MeshService::injectAsReceived`.

    `mode`:
      - "text":       inject a text message `body` from `from_node`.
      - "raw":        inject `payload_hex` on `portnum`.
      - "admin":      inject a set_owner admin (`long_name`/`short_name`, optional `session_hex`);
                      pair with pki=true + public_key_b64 to exercise the PKC-admin path.
      - "ciphertext": inject `ciphertext_hex` verbatim as encrypted bytes (fed to the decoder).
      - "fuzz":       inject `fuzz_count` random/malformed frames (decode-path robustness testing).

    `from_node` is the sender to forge (from==0 is dropped like real RX). `to` defaults to the
    target's own num. `encrypt` (default true) channel-AES-CTR-encrypts the payload so the firmware
    decrypts it as if received; set false to inject already-decoded (needed with `pki`).
    `channel_index` selects which configured channel's key/hash to use.

    Returns: {ok, target, channel, channel_hash, injected, frames:[{from,to,id,portnum,bytes,...}]}
    """
    return inject_mod.inject_frame(
        mode=mode,
        body=body,
        portnum=portnum,
        payload_hex=payload_hex,
        ciphertext_hex=ciphertext_hex,
        long_name=long_name,
        short_name=short_name,
        session_hex=session_hex,
        from_node=from_node,
        to=to,
        channel_index=channel_index,
        packet_id=packet_id,
        want_response=want_response,
        encrypt=encrypt,
        pki=pki,
        public_key_b64=public_key_b64,
        fuzz_count=fuzz_count,
        fuzz_seed=fuzz_seed,
        confirm=confirm,
        port=port,
    )


@app.tool()
def reboot(port: str | None = None, confirm: bool = False, seconds: int = 10) -> dict[str, Any]:
    """Reboot the connected node in `seconds` seconds. Requires confirm=True.

    Returns:
        {ok: true, rebooting_in_s: int}
    """
    return admin.reboot(port=port, confirm=confirm, seconds=seconds)


# ---------- RF compliance oracle (RTL-SDR) ---------------------------------


@sdr_tool()
def rf_scan(
    center_freq_hz: float,
    span_khz: float = 1000.0,
    duration_s: float = 2.0,
    gain: float | str = "auto",
    device_index: int = 0,
) -> dict[str, Any]:
    """Capture and characterize RF activity at a frequency with an RTL-SDR. No
    Meshtastic device involved — use for a pre-test channel-occupancy check
    ("is this band already busy") or to probe a frequency by hand. See
    `rf_confirm_tx` for cross-checking a live device's actual TX against its
    own configured region/preset.

    Returns:
        {center_hz, sample_rate_hz, duration_s, active_windows: [{start_s, duration_s}],
         duty_cycle_pct_in_capture, occupied_bandwidth_hz, peak_freq_offset_hz,
         peak_power_db, noise_floor_db_estimate}
    """
    return rf_oracle.scan(
        center_freq_hz,
        span_khz=span_khz,
        duration_s=duration_s,
        gain=gain,
        device_index=device_index,
    )


@sdr_tool()
def rf_confirm_tx(
    text: str,
    channel_index: int = 0,
    port: str | None = None,
    window_s: float = 5.0,
    gain: float | str = "auto",
    device_index: int = 0,
    tx_confirm_lookback_s: float = 60.0,
) -> dict[str, Any]:
    """Send a text message while an RTL-SDR captures the frequency the device's
    *own configured* region/preset/channel predict it should transmit on —
    independent ground truth, not the device's self-reported packet log.

    Catches the class of bug firmware can never self-report: TX reported as
    queued OK (`send_result.ok`) but no RF actually left the antenna (dead PA,
    disconnected antenna, a region/frequency miscalculation). Also flags
    off-frequency or off-region emission and reports the measured occupied
    bandwidth alongside the regulatory duty-cycle/power limit for the
    device's region.

    **Delayed TX under airtime pressure looks identical to a dropped send.**
    Firmware enforces its own channel-utilization budget; a queued packet can
    sit for tens of seconds before actually transmitting if you (or the mesh)
    have been generating a lot of recent traffic — empirically observed here
    as 40-70s delays after back-to-back test calls. A single `ok=False` /
    `silent_tx_suspected=True` result right after a burst of test sends is NOT
    conclusive; space calls out, or re-check with `rf_scan` shortly after,
    before concluding TX is actually broken.

    `firmware_self_reported_tx` in the result is best-effort only, NOT a
    reliable cross-check: the `meshtastic` library discards the local echo of
    a packet you just sent (firmware omits the redundant `from` field on that
    echo, and the library drops anything missing it rather than publishing a
    pubsub event) — so for a plain broadcast this is `False` almost always,
    regardless of whether the send worked. `ok` / `measured.rf_observed` (the
    SDR evidence) is what `silent_tx_suspected` is actually based on.
    `tx_confirm_lookback_s` (default 60s) controls how far back this best-
    effort check searches the packet log — deliberately much longer than
    `window_s` since checking the log is cheap, unlike extending the live SDR
    capture. For a real independent cross-node check, use a second Meshtastic
    device on the same channel and inspect its own recorder — that receive
    event is never suppressed since `from` is genuinely populated there (same
    delayed-TX caveat still applies: watch long enough).

    Needs an RTL-SDR (`doctor` shows whether the sdr capability is active).
    Coverage is limited to ~24MHz-1766MHz (R820T/R820T2 tuner range) —
    LORA_24 (2.4GHz) can't be checked with an RTL-SDR. This is a dev-loop
    regression check with an uncalibrated power reference, not a substitute
    for certified EMC-lab compliance testing.

    Returns:
        {ok: bool, silent_tx_suspected: bool, predicted: {region, freq_mhz, bw_khz,
         sf, cr, duty_cycle_limit_pct, power_limit_dbm}, measured: {rf_observed,
         matched_window, occupied_bandwidth_hz, freq_offset_from_predicted_hz,
         in_region_band_fraction, all_active_windows_in_capture,
         duty_cycle_pct_in_capture}, firmware_self_reported_tx (best-effort,
         see above), send_result, capture, caveat}
    """
    return rf_oracle.confirm_tx(
        text,
        channel_index=channel_index,
        port=port,
        window_s=window_s,
        gain=gain,
        device_index=device_index,
        tx_confirm_lookback_s=tx_confirm_lookback_s,
    )


# ---------- PA calibration bench (ImmersionRC RF Power Meter v2) ------------


@app.tool()
def pa_meter_status(meter_port: str | None = None) -> dict[str, Any]:
    """Detect the ImmersionRC RF Power Meter and report a live reading — the
    bench-instrument equivalent of `recorder_status`. Read-only; no Meshtastic
    device involved.

    Use it to confirm the meter is connected and awake before a `pa_sweep`
    (it auto-powers-off on a battery timeout and vanishes from USB), and to see
    the current noise floor / any signal it's presently reading.

    Returns:
        {present: bool, port, version, stored_freq_mhz, current_avg_dbm,
         current_peak_dbm}  — or {present: false, detail} when none is attached.
    """
    return pa_sweep_mod.status(meter_port=meter_port)


@app.tool()
def pa_measure(
    band: str,
    samples: int = 20,
    interval_s: float = 0.05,
    attenuator_db: float = 0.0,
    meter_port: str | None = None,
    peak: bool = False,
) -> dict[str, Any]:
    """Passively read the power the meter currently sees at `band` — no Meshtastic
    device driven. Activates the band's calibration curve, takes `samples`
    readings, returns min/mean/max in dBm (corrected for `attenuator_db`, the pad
    between the source and the meter).

    `band` accepts a Meshtastic region name (`"US"`, `"EU_868"`, `"EU_433"`,
    `"JP"`, ...) or a bare MHz value (`"868"`); it snaps to the meter's nearest
    stored calibration point. Use it to read the noise floor, verify a signal
    generator, or spot-check a TX something else is keying. Note `attenuator_db`
    is added to every reading, so it inflates a noise-floor read by the pad value
    — pass `attenuator_db=0` for the meter's raw floor. For a closed-loop node PA
    sweep use `pa_sweep`.

    Returns:
        {band, requested_center_mhz, meter_cal_mhz, kind, attenuator_db, samples,
         min_dbm, mean_dbm, max_dbm}
    """
    return pa_sweep_mod.measure(
        band,
        samples=samples,
        interval_s=interval_s,
        attenuator_db=attenuator_db,
        meter_port=meter_port,
        peak=peak,
    )


@app.tool()
def pa_sweep(
    powers: list[int],
    band: str | None = None,
    port: str | None = None,
    meter_port: str | None = None,
    channel_index: int = 0,
    attenuator_db: float = 0.0,
    burst_repeat: int = 3,
    tx_linger_s: float | None = None,
    settle_s: float = 1.5,
    reboot_between_steps: bool = False,
    override_duty_cycle: bool = True,
    restore_config: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Closed-loop PA calibration: step `lora.tx_power` through `powers` and
    measure the actual power off the node's PA with the ImmersionRC meter,
    producing a configured-vs-measured table and a compression/saturation
    analysis. Requires confirm=True.

    Destructive: for each step it writes `lora.tx_power` on the node, keys real
    ~200 B broadcast bursts onto the mesh (an idle node rarely transmits), and
    optionally reboots between steps (`reboot_between_steps=True` for pre-2.8.0
    firmware that doesn't apply LoRa config live). It captures the meter's noise
    floor first, then keeps only TX-active samples (floor + margin) per step.

    `tx_linger_s` is how long each broadcast holds the port open so the firmware's
    ~4 s politeness delay + airtime finish before close drops the TX; it is paid
    per burst per step and dominates wall-clock. Leave it `None` (default) to
    **auto-derive** it from the node's live preset time-on-air — a fast preset
    gets a short linger, LONG_SLOW gets a long enough one, no clipping and no
    tuning. Pass a number to override. The value used is echoed as `tx_linger_s`
    in the result.

    Instrument safety: pick `attenuator_db` so the highest configured power minus
    the pad stays under the meter's +31 dBm absolute max — the sweep refuses to
    run otherwise. `band` defaults to the node's configured region (a Meshtastic
    region name like `"US"` / `"EU_868"`, or a bare MHz value). On EU_868 the
    duty-cycle limit is overridden for the run and restored after (with
    `override_duty_cycle=True`, the default). The original `tx_power` and
    duty-cycle override are restored on exit unless `restore_config=False`; each
    restore is independent, and any that fails (e.g. the port was busy) is
    reported in `restore_errors` instead of silently leaving state changed.

    A step with no TX-active sample (see `silent_steps_dbm`) can be a dead PA —
    but under airtime pressure a queued packet can also transmit after the
    sampling window closes, so re-run spaced out before concluding a step is
    truly silent. Uncalibrated bench check (~±0.5 dB + hand-entered pad), not a
    certified measurement.

    Returns:
        {band, region, requested_center_mhz, meter_cal_mhz, attenuator_db,
         tx_linger_s, floor_dbm, floor_margin_db, table: [{configured_dbm, measured_avg_dbm,
         measured_peak_dbm, delta_db, active_samples, total_samples, rf_observed}],
         curve: {points, saturation_dbm, max_measured_dbm,
         max_measured_at_configured_dbm, offset_at_min_db, monotonic},
         silent_steps_dbm, config_restored, restore_errors, caveat}
    """
    return pa_sweep_mod.sweep(
        powers,
        band=band,
        port=port,
        meter_port=meter_port,
        channel_index=channel_index,
        attenuator_db=attenuator_db,
        burst_repeat=burst_repeat,
        tx_linger_s=tx_linger_s,
        settle_s=settle_s,
        reboot_between_steps=reboot_between_steps,
        override_duty_cycle=override_duty_cycle,
        restore_config=restore_config,
        confirm=confirm,
    )


@app.tool()
def shutdown(port: str | None = None, confirm: bool = False, seconds: int = 10) -> dict[str, Any]:
    """Shut down the connected node in `seconds` seconds. Requires confirm=True.

    Returns:
        {ok: true, shutting_down_in_s: int}
    """
    return admin.shutdown(port=port, confirm=confirm, seconds=seconds)


@app.tool()
def factory_reset(
    port: str | None = None, confirm: bool = False, full: bool = False
) -> dict[str, Any]:
    """Factory-reset the connected node. Requires confirm=True.

    `full=True` also wipes device identity/keys (not just config).

    Returns:
        {ok: true}
    """
    return admin.factory_reset(port=port, confirm=confirm, full=full)


@app.tool()
def send_input_event(
    event_code: int | str,
    kb_char: int = 0,
    touch_x: int = 0,
    touch_y: int = 0,
    port: str | None = None,
) -> dict[str, Any]:
    """Inject an InputBroker event (button / key / gesture) into the device UI.

    Drives the same code path as a physical button press. Accepts a numeric
    event code (0..255) or a name like `"RIGHT"`, `"SELECT"`, `"FN_F1"`.

    Common codes: SELECT=10, UP=17, DOWN=18, LEFT=19, RIGHT=20, CANCEL=24,
    BACK=27, FN_F1..F5=241..245.

    Returns:
        {ok: true, event_code: int, kb_char: int}
    """
    return admin.send_input_event(
        event_code=event_code,
        kb_char=kb_char,
        touch_x=touch_x,
        touch_y=touch_y,
        port=port,
    )


@app.tool()
def capture_screen(role: str | None = None, ocr: bool = True) -> dict[str, Any]:
    """Grab a frame from the USB webcam pointed at the device screen.

    Returns PNG bytes (base64), optional OCR text, and backend metadata.
    Requires the `[ui]` extras (opencv-python-headless) and a camera
    configured via `MESHTASTIC_UI_CAMERA_DEVICE[_<ROLE>]`. Falls back to a
    1×1 black PNG from the null backend when no camera is configured.
    """
    import base64

    from . import camera as camera_mod

    cam = camera_mod.get_camera(role)
    try:
        png = cam.capture()
    finally:
        cam.close()

    result: dict[str, Any] = {
        "backend": cam.name,
        "bytes": len(png),
        "image_base64": base64.b64encode(png).decode("ascii"),
    }
    if ocr:
        from . import ocr as ocr_mod

        result["ocr_backend"] = ocr_mod.backend_name()
        result["ocr_text"] = ocr_mod.ocr_text(png)
    return result


# ---------- USB power control (uhubctl) -----------------------------------


@app.tool()
def uhubctl_list() -> list[dict[str, Any]]:
    """List every USB hub + per-port device attachment as seen by `uhubctl`.

    Read-only — no confirm required. Each hub entry includes its location
    (`1-1.3`), descriptor, whether it supports Per-Port Power Switching,
    and a list of populated ports with VID:PID of attached devices.
    Useful for pre-flight checks before a destructive power-cycle call.
    """
    from . import uhubctl as uhubctl_mod

    return uhubctl_mod.list_hubs()


@app.tool()
def uhubctl_power(
    action: str,
    location: str | None = None,
    port: int | None = None,
    role: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power a USB hub port on or off via `uhubctl -a on|off`.

    Target the port by either (`location`, `port`) — raw uhubctl syntax,
    e.g. `location="1-1.3", port=2` — OR by `role` ("nrf52", "esp32s3").
    Role lookup honors `MESHTASTIC_UHUBCTL_LOCATION_<ROLE>` +
    `_PORT_<ROLE>` env vars first, falls back to VID auto-detection.

    `action="off"` requires `confirm=True` (destructive — the attached
    device will immediately disappear from the OS).
    """
    from . import uhubctl as uhubctl_mod

    action_lower = action.lower()
    if action_lower not in {"on", "off"}:
        raise ValueError(f"action must be 'on' or 'off', got {action!r}")
    if action_lower == "off" and not confirm:
        raise uhubctl_mod.UhubctlError("uhubctl_power action='off' requires confirm=True")
    loc, p = _resolve_uhubctl_target(location, port, role)
    if action_lower == "on":
        return uhubctl_mod.power_on(loc, p)
    return uhubctl_mod.power_off(loc, p)


@app.tool()
def uhubctl_cycle(
    location: str | None = None,
    port: int | None = None,
    role: str | None = None,
    delay_s: int = 2,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power a USB hub port off, wait `delay_s` seconds, then on.

    The typical hard-reset sequence — shorter than off+on as two RPCs
    because uhubctl handles the timing in-process. Target by (location,
    port) or by role (see `uhubctl_power`). Requires `confirm=True`.
    """
    from . import uhubctl as uhubctl_mod

    if not confirm:
        raise uhubctl_mod.UhubctlError("uhubctl_cycle requires confirm=True")
    if delay_s < 0 or delay_s > 60:
        raise ValueError(f"delay_s must be 0..60, got {delay_s}")
    loc, p = _resolve_uhubctl_target(location, port, role)
    return uhubctl_mod.cycle(loc, p, delay_s=delay_s)


def _resolve_uhubctl_target(
    location: str | None, port: int | None, role: str | None
) -> tuple[str, int]:
    """Shared arg-resolution for uhubctl_power + uhubctl_cycle."""
    from . import uhubctl as uhubctl_mod

    if role is not None:
        if location is not None or port is not None:
            raise ValueError("pass either `role` OR (`location` + `port`), not both")
        return uhubctl_mod.resolve_target(role)
    if location is None or port is None:
        raise ValueError("must pass `role` or both `location` and `port`")
    return (location, int(port))


# ---------- Direct hardware tools -----------------------------------------


@app.tool()
def esptool_chip_info(port: str) -> dict[str, Any]:
    """Run `esptool flash_id` and return chip, MAC, crystal, and flash size.

    Read-only — no confirm required. Prefer this over parsing pio upload logs
    when you just want to identify the chip.
    """
    return hw_tools.esptool_chip_info(port)


@app.tool()
def esptool_erase_flash(port: str, confirm: bool = False) -> dict[str, Any]:
    """Full-chip erase via `esptool erase_flash`. Leaves the device unbootable.

    Prefer `erase_and_flash` which also writes firmware. Use this only for
    recovery when a device is in a weird state. Requires confirm=True.
    """
    return hw_tools.esptool_erase_flash(port, confirm=confirm)


@app.tool()
def esptool_raw(args: list[str], port: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Pass-through to `esptool`. Destructive subcommands (write_flash,
    erase_flash, erase_region, merge_bin) require confirm=True.

    Prefer the high-level `pio_flash` / `erase_and_flash` / `update_flash`
    tools where possible — they know board-specific offsets and protocols.

    Security: `args` is passed directly to the esptool binary. All arguments
    must come from a trusted source — do not allow untrusted content (e.g.
    mesh packet payloads) to flow into argument lists.
    """
    return hw_tools.esptool_raw(args, port=port, confirm=confirm)


@app.tool()
def nrfutil_dfu(port: str, package_path: str, confirm: bool = False) -> dict[str, Any]:
    """DFU-flash a .zip package to an nRF52840 via `nrfutil dfu serial`.

    Prefer `pio_flash` for flashing firmware built from this repo — pio handles
    the DFU invocation automatically. Use this tool when flashing a pre-built
    release zip or a custom bootloader. Requires confirm=True.
    """
    return hw_tools.nrfutil_dfu(port, package_path, confirm=confirm)


@app.tool()
def nrfutil_raw(args: list[str], confirm: bool = False) -> dict[str, Any]:
    """Pass-through to `nrfutil`. dfu/settings subcommands require confirm=True.

    Security: `args` is passed directly to the nrfutil binary. All arguments
    must come from a trusted source — do not allow untrusted content to flow
    into argument lists.
    """
    return hw_tools.nrfutil_raw(args, confirm=confirm)


@app.tool()
def picotool_info(port: str | None = None) -> dict[str, Any]:
    """Run `picotool info -a`. Requires the RP2040 to be in BOOTSEL mode
    (hold BOOTSEL button while plugging in, or call `touch_1200bps` if the
    firmware supports 1200bps-reset)."""
    return hw_tools.picotool_info(port=port)


@app.tool()
def picotool_load(uf2_path: str, confirm: bool = False) -> dict[str, Any]:
    """Load a UF2 to a Pico in BOOTSEL mode via `picotool load -x -t uf2`.

    Prefer `pio_flash` for flashing firmware built from this repo.
    Requires confirm=True.
    """
    return hw_tools.picotool_load(uf2_path, confirm=confirm)


@app.tool()
def picotool_raw(args: list[str], confirm: bool = False) -> dict[str, Any]:
    """Pass-through to `picotool`. load/reboot/save/erase require confirm=True.

    Security: `args` is passed directly to the picotool binary. All arguments
    must come from a trusted source — do not allow untrusted content to flow
    into argument lists.
    """
    return hw_tools.picotool_raw(args, confirm=confirm)


# ---------- Persistent device-log capture (recorder) ----------------------
#
# The recorder is autouse — it starts at server import and continuously
# writes every meshtastic pubsub event to JSONL files under .mtlog/. These
# tools are query-only over those files, plus a few lifecycle controls.


@app.tool()
def logs_window(
    start: str = "-15m",
    end: str = "now",
    grep: str | None = None,
    level: str | None = None,
    tag: str | None = None,
    port: str | None = None,
    max_lines: int = 200,
) -> dict[str, Any]:
    """Recent firmware log lines from the persistent recorder.

    Filters by time window, regex over the line, level (single or
    pipe-separated set like "WARN|ERROR|CRIT"), thread-name tag, and
    interface port. Returns up to max_lines most-recent matches.

    Time strings: "-15m", "-2h", "-3d", "now", or ISO 8601.

    Note: lines arriving via the LogRecord protobuf path (when
    set_debug_log_api(True) is on) come without level prefix — the
    meshtastic Python lib drops record.level before fan-out. For those,
    `level` filter won't match; use `grep` instead.
    """
    return log_query.logs_window(
        start=start,
        end=end,
        grep=grep,
        level=level,
        tag=tag,
        port=port,
        max_lines=max_lines,
    )


@app.tool()
def telemetry_timeline(
    window: str = "1h",
    variant: str = "local",
    field: str = "free_heap",
    port: str | None = None,
    max_points: int = 200,
) -> dict[str, Any]:
    """Time series of one telemetry field, downsampled to <= max_points.

    `variant` ∈ device, local, environment, power, airQuality, health, host.
    `field` accepts snake_case or camelCase; common aliases (free_heap ↔
    heap_free_bytes) are normalized.

    Returns slope_per_min (linear-regression slope, units/minute) so a
    leak detector can read one number — negative slope on free_heap over
    a long window indicates a real leak.

    LocalStats variant ("local") cadence is ~60 s (whatever the device's
    `device_update_interval` is set to), so a 1 h window gives ~60 raw
    points. Bucket-mean downsampling preserves shape.
    """
    return log_query.telemetry_timeline(
        window=window,
        variant=variant,
        field=field,
        port=port,
        max_points=max_points,
    )


@app.tool()
def packets_window(
    start: str = "-5m",
    end: str = "now",
    portnum: str | None = None,
    from_node: str | None = None,
    to_node: str | None = None,
    max: int = 200,
) -> dict[str, Any]:
    """Recent mesh packets recorded by the recorder.

    Each row is a summary (portnum, from/to, hop_limit, RSSI/SNR, payload
    size + first 64 bytes hex) — full payload bytes are not stored.
    `portnum` accepts a pipe-separated set like "TEXT_MESSAGE_APP|POSITION_APP".
    """
    return log_query.packets_window(
        start=start,
        end=end,
        portnum=portnum,
        from_node=from_node,
        to_node=to_node,
        max=max,
    )


@app.tool()
def events_window(
    start: str = "-1h",
    end: str = "now",
    kind: str | None = None,
    max: int = 200,
) -> dict[str, Any]:
    """Return recorder events: connection lifecycle, node updates, and `mark_event` markers.

    `kind` ∈ recorder_start, recorder_pause, recorder_resume,
    connection_established, connection_lost, node_updated, mark.
    Pipe-separated sets ("connection_lost|connection_established") work.
    """
    return log_query.events_window(start=start, end=end, kind=kind, max=max)


@app.tool()
def mark_event(
    label: str,
    note: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop a named marker into events.jsonl AND logs.jsonl.

    Useful for aligning a timeline around a known stimulus: call before
    and after a stress workload, then query telemetry_timeline /
    logs_window with the markers' timestamps as bounds.

    The marker also lands in logs.jsonl with level=MARK so a single
    grep over logs picks it up.
    """
    return get_recorder().mark_event(label=label, note=note, data=data)


@app.tool()
def recorder_status() -> dict[str, Any]:
    """Return recorder runtime info: running, paused, file sizes, last_ts per stream.

    Use this to sanity-check that capture is working before you trust a
    `logs_window` / `telemetry_timeline` result.
    """
    return get_recorder().status()


@app.tool()
def recorder_pause(reason: str | None = None) -> dict[str, Any]:
    """Pause writes to all four streams. Pubsub subscriptions stay active —
    we just drop events on the floor while paused. Resume with `recorder_resume`.

    Use when capturing a known-good baseline that you don't want to
    pollute with pre-test noise. Default state is recording; this is
    rarely needed.
    """
    get_recorder().pause(reason=reason)
    return {"ok": True, "paused": True, "reason": reason}


@app.tool()
def recorder_resume() -> dict[str, Any]:
    """Resume writes after `recorder_pause`. No-op if already running."""
    get_recorder().resume()
    return {"ok": True, "paused": False}


@app.tool()
def recorder_export(
    start: str,
    end: str,
    dest_dir: str,
    streams: list[str] | None = None,
) -> dict[str, Any]:
    """Bundle a slice of the recorder's streams into `dest_dir`.

    Writes one uncompressed JSONL per requested stream (logs / telemetry /
    packets / events). Useful for: attaching to a bug report, feeding a
    notebook, or backfilling Datadog after the fact.
    """
    return log_query.export(
        start=start,
        end=end,
        dest_dir=dest_dir,
        streams=streams,
    )


# ---------- Replay: simulated Meshtastic TCP device -----------------------

# Serve a capture (SQLite DB, recorder JSONL, or a generated synthetic mesh) as
# a fake radio over TCP. The inverse of the recorder. An app/AVD connects to the
# listen port and sees a live mesh. Sessions run in background threads.


def _load_replay_capture(
    source: str,
    *,
    kind: str,
    limit_nodes: int,
    sim_nodes: int,
    sim_days: int,
    sim_seed: int,
    sim_start: int | None,
    channels: list[dict[str, Any]] | None = None,
    sim_profile: str | dict[str, Any] | None = None,
) -> Any:
    """Resolve `source`/`kind` into a Capture for the replay engine."""
    if kind == "sim" or source in ("sim", *replay_sim.PRESETS):
        preset = "meshcon" if source in ("sim", "meshcon") else source
        override: dict[str, Any] | None = None
        if sim_profile is not None:
            if isinstance(sim_profile, str):
                import json

                sim_profile = json.loads(sim_profile)
            if not isinstance(sim_profile, dict):
                raise ValueError(
                    "sim_profile must be a JSON object / dict of profile overrides "
                    "(preset base is chosen via `source`; file paths are not accepted)"
                )
            override = sim_profile
        prof = replay_sim.preset_profile(preset, override)
        return replay_sim.generate(
            nodes=sim_nodes, days=sim_days, seed=sim_seed, start=sim_start, profile=prof
        )
    if kind == "jsonl" or source.endswith(".jsonl"):
        return replay_capture.from_recorder_jsonl(source)
    return replay_capture.from_sqlite(source, limit_nodes=limit_nodes, channel_specs=channels)


@app.tool()
def replay_start(
    source: str = "meshcon",
    kind: str = "auto",
    host: str = "0.0.0.0",
    port: int = 4403,
    speed: float = 1.0,
    rate: float | None = None,
    max_gap: float = 20.0,
    start: str | None = None,
    end: str | None = None,
    loop: bool = False,
    limit_nodes: int = 200,
    node_delay: float = 0.01,
    channels: list[dict[str, Any]] | None = None,
    announce_interval: float = 0.0,
    modem_preset: str = "LONG_FAST",
    firmware_edition: str = "VANILLA",
    observer_lat: int | None = None,
    observer_lon: int | None = None,
    sim_nodes: int = 800,
    sim_days: int = 3,
    sim_seed: int = 1337,
    sim_profile: str | dict[str, Any] | None = None,
    fuzz: str | dict[str, Any] | None = None,
    fuzz_seed: int = 0,
) -> dict[str, Any]:
    """Start a simulated Meshtastic TCP device that streams a capture to an app.

    The inverse of the recorder: instead of capturing a live mesh, this serves
    one. An app (or AVD at `10.0.2.2:<port>`, or the meshtastic Python lib)
    connects to `host:port`, does the want-config handshake, and receives a
    paced stream of packets restamped to "now" — behaving like a real radio.

    `source` / `kind`:
      - `meshcon` / `sim`     → generate a synthetic MeshCon mesh (no file).
      - `burningman` / `defcon` → generate a calibrated event scenario (playa /
                                convention geo, channels, RF observer model,
                                encrypted+MQTT mix, and scripted traffic spikes).
      - a `*.db` / `*.db.gz`  → SQLite capture (Burning Man / DEF CON / MeshCon
                                schema; full-fidelity payloads). `kind="sqlite"`.
      - a `*.jsonl`           → recorder packets.jsonl (best-effort, truncated
                                payloads). `kind="jsonl"`.
      - `kind="auto"` (default) infers from the source string.

    Pacing: `rate` (steady packets/sec, ignores capture timing) takes priority;
    otherwise `speed` multiplies the original cadence, capped by `max_gap` idle.
    `start`/`end` (ISO-8601 UTC) window the capture. `loop` restarts at the end.
    `limit_nodes` caps the node DB (file sources). `sim_nodes`/`sim_days`/
    `sim_seed` size and seed the synthetic generator; `sim_profile` tunes it
    further for sim/preset sources — a dict (or JSON-object string) of profile
    overrides deep-merged over the `source` preset. Use it to enable/shape
    features the presets leave off, e.g. an ATAK squad
    `{"tak": {"team_nodes": 6, "wire": "v2"}}`, a scripted spike
    `{"spikes": [{"start_h": 20, "hours": 2, "text_x": 8}]}`, or the RF gateway
    model `{"observer": {"enabled": true, "loss_floor": 0.5, "mqtt_fraction": 0.3}}`.
    A preset base is chosen via `source`; `sim_profile` is never a file path.

    `channels` (SQLite sources) is a caller-supplied list of channels that routes
    packets by their OTA channel hash and advertises the real PSKs so the app
    live-decrypts encrypted packets. Each entry: `{"name": str, "psk": "<base64>",
    "primary": bool, "ota_hashes": [int, ...], "catch_all": bool}` (psk/ota_hashes/
    catch_all optional; a channel's hash is derived from name+psk if not given,
    and `catch_all` channels receive packets matching no hash). Omit for
    name-column channels with placeholder keys.

    `announce_interval` > 0 adds a "Replay Clock" node that posts a kickoff and a
    periodic "ETA — done/total" progress message to the busiest channel, so you
    can see from inside the app that it's a replay. `modem_preset` sets the
    advertised LoRa preset (e.g. `LONG_FAST`, `SHORT_TURBO`); `firmware_edition`
    sets the app's event banner (`VANILLA`, `DEFCON`, `BURNING_MAN`, `HAMVENTION`,
    …). `observer_lat`/`observer_lon` (1e-7 degrees) place the *connected* node
    (the app's "you are here" on the map) — distinct from the sim's RF gateway
    observer, which is configured via `sim_profile["observer"]`. Default is the
    capture's median position so the map and node distances look right.

    `fuzz` turns the stream hostile (fault injection + bad actors): a preset name
    (`light`, `parser`, `adversary`, `chaos`) or a dict of overrides (optionally
    with a `preset` base). `light` = rare corruption + loss; `parser` = malformed
    bodies / bad values to exercise the decoder; `adversary` = spoofing, flooding,
    forged ACKs, rogue ADMIN, evil-twin impersonation; `chaos` = everything incl.
    frame corruption. Seeded by `fuzz_seed` so a crash reproduces. Fuzz activity
    (counts + recent events) is reported under `fuzz` in `replay_status`. List
    presets with `replay_fuzz_presets`.

    Returns the session status (id, listen address, totals). Poll with
    `replay_status`, tear down with `replay_stop`.
    """
    sim_start = None
    s_epoch = _parse_iso_epoch(start) if start else None
    e_epoch = _parse_iso_epoch(end) if end else None
    is_sim = kind == "sim" or source in ("sim", *replay_sim.PRESETS)
    if is_sim and s_epoch is None:
        # default the synthetic capture to end "now" so a fresh app sees recent data
        import time as _t

        sim_start = int(_t.time()) - sim_days * 86400
    cap = _load_replay_capture(
        source,
        kind=kind,
        limit_nodes=limit_nodes,
        sim_nodes=sim_nodes,
        sim_days=sim_days,
        sim_seed=sim_seed,
        sim_start=sim_start,
        channels=channels,
        sim_profile=sim_profile,
    )
    params = ReplayParams(
        host=host,
        port=port,
        speed=speed,
        rate=rate,
        max_gap=max_gap,
        start=s_epoch,
        end=e_epoch,
        loop=loop,
        limit_nodes=limit_nodes,
        node_delay=node_delay,
        announce_interval=announce_interval,
        modem_preset=modem_preset,
        firmware_edition=firmware_edition,
        observer_lat=observer_lat,
        observer_lon=observer_lon,
        fuzz=replay_fuzz.from_spec(fuzz, seed=fuzz_seed),
    )
    try:
        return get_replay_manager().start(cap, params)
    except replay_engine.PortInUseError as exc:
        return {"error": str(exc)}


@app.tool()
def replay_inject(
    session_id: str,
    kind: str,
    args: dict[str, Any] | None = None,
    from_node: int | None = None,
    to_node: int | None = None,
    channel: str = "LongFast",
    count: int = 1,
    fuzz: bool = False,
) -> dict[str, Any]:
    """Inject packet(s) into a running replay session — drive app features on demand.

    Targeted counterpart to the fuzzer's random campaigns: build a packet from a
    high-level `kind` + `args` and emit it onto the live connection (same send
    path as the stream). `kind`:
      - `waypoint`  args: `lat`, `lon`, `name`, `geofence_radius` (m), `bbox`
                    `[south,west,north,east]`, `notify_on_enter`, `notify_on_exit`,
                    `notify_favorites_only`, `icon`
      - `position`  args: `lat`, `lon`, `altitude`
      - `text`      args: `body`
      - `nodeinfo`  args: `id`, `long_name`, `short_name`, `hw_model`, `role`
      - `raw`       args: `portnum`, `payload_hex`

    `from_node` is the source node num (default a synthetic injector node).
    `fuzz=True` runs each packet through the session's active fuzz mutator first
    (inject a deliberately malformed packet). `count` repeats the inject.

    Example (geofence): start a session, then
    `replay_inject(sid, "waypoint", {"lat":37.0,"lon":-122.0,"geofence_radius":500,
    "notify_on_enter":true,"notify_on_exit":true,"name":"Test"})` followed by
    `replay_inject(sid, "position", {"lat":37.0,"lon":-122.0}, from_node=<tracker>)`.
    """
    frm = from_node if from_node is not None else 0x0A1B2C3D  # synthetic injector
    to = to_node if to_node is not None else replay_build.BROADCAST
    pkts = [
        replay_build.from_kind(kind, args or {}, from_node=frm, to_node=to)
        for _ in range(max(1, count))
    ]
    return get_replay_manager().inject(session_id, pkts, channel=channel, fuzz=fuzz)


@app.tool()
def replay_inject_beacon(
    session_id: str,
    message: str = "Mesh beacon — join the network",
    *,
    from_node: int | None = None,
    channel: str = "LongFast",
    offer_channel_name: str = "",
    offer_channel_psk_hex: str = "",
    offer_region: str = "US",
    offer_preset: str = "LONG_FAST",
    count: int = 1,
) -> dict[str, Any]:
    """Inject a MESH_BEACON_APP packet (portnum 37) into a running replay session.

    Convenience wrapper around `replay_inject(sid, "beacon", …)` that lets the
    Apple app (and others) exercise the "Local Mesh Discovery" flow — capturing
    beacons, auto-adding beacon-advertised presets/channels to a scan, and
    offering "switch to this channel" — without real beaconing hardware.

    `message` is the human-readable beacon text (max 100 bytes on real firmware).
    `offer_channel_name` / `offer_channel_psk_hex` optionally advertise a channel
    the client app can offer the user to switch to. `offer_region` and
    `offer_preset` advertise the LoRa region/preset of the beaconed mesh.
    """
    frm = from_node if from_node is not None else 0x0A1B2C3D
    pkts = [
        replay_build.from_kind(
            "beacon",
            {
                "message": message,
                "offer_channel_name": offer_channel_name,
                "offer_channel_psk_hex": offer_channel_psk_hex,
                "offer_region": offer_region,
                "offer_preset": offer_preset,
            },
            from_node=frm,
            to_node=replay_build.BROADCAST,
        )
        for _ in range(max(1, count))
    ]
    return get_replay_manager().inject(session_id, pkts, channel=channel)


@app.tool()
def replay_inject_fileinfo(
    session_id: str,
    file_name: str,
    size_bytes: int = 0,
    count: int = 1,
) -> dict[str, Any]:
    """Inject a raw FileInfo FromRadio message into a running replay session.

    Unlike `replay_inject` (which wraps a MeshPacket for mesh traffic), FileInfo is a
    handshake-only FromRadio oneof (STATE_SEND_FILEMANIFEST) with no MeshPacket envelope --
    real firmware only ever sends it during the initial config handshake. Most app-side
    handlers don't gate on handshake state though, so this lets you exercise that code path
    on demand, e.g. to fuzz-test unbounded accumulation (send many with `count`) or
    malformed/adversarial entries (huge `file_name`, negative `size_bytes`) in a
    long-running session rather than only at connect time.

    `count` repeats the inject (each with a distinct file_name suffix so entries don't
    collide) -- useful for probing whether a client caps its file manifest.
    """
    msgs = [
        replay_build.fromradio_from_kind(
            "fileinfo",
            {
                "file_name": file_name if count == 1 else f"{file_name}.{i}",
                "size_bytes": size_bytes,
            },
        )
        for i in range(max(1, count))
    ]
    return get_replay_manager().inject_fromradio(session_id, msgs)


@app.tool()
def replay_inject_traceroute(
    session_id: str,
    destination_node: int,
    *,
    route: list[int] | None = None,
    snr_towards: list[int] | None = None,
    route_back: list[int] | None = None,
    snr_back: list[int] | None = None,
    from_node: int | None = None,
    channel: str = "LongFast",
) -> dict[str, Any]:
    """Inject a TRACEROUTE_APP RouteDiscovery packet into a running replay session.

    Convenience wrapper that lets you test the traceroute UI (hop list, SNR
    colouring, map flyover) without real hardware.

    `destination_node` is the node num the traceroute is addressed *from*
    (i.e. the node "responding" — the source of the RouteDiscovery reply).
    `route` is the list of node nums along the path (destination last); if
    omitted a synthetic multi-hop route (origin → relay → destination) is used
    so the hop-list/SNR UI has something meaningful to render. `snr_towards` /
    `snr_back` are per-hop SNR values; if omitted realistic random values are
    generated.

    The replay engine also answers live traceroute *requests* sent by a connected
    client automatically — this tool lets you push an unsolicited RouteDiscovery
    to exercise the display path.
    """
    frm = from_node if from_node is not None else destination_node
    if route:
        effective_route = route
    elif frm != destination_node:
        effective_route = [frm, destination_node]
    else:
        # Only session_id + destination_node given: fabricate a plausible
        # three-hop path through a synthetic origin and relay so the client's
        # hop list / SNR colouring has more than a single degenerate node.
        synthetic_origin = 0x0A1B2C3D
        synthetic_relay = 0x0A1B2C3E
        effective_route = [synthetic_origin, synthetic_relay, destination_node]
    pkts = [
        replay_build.from_kind(
            "traceroute",
            {
                "route": effective_route,
                "snr_towards": snr_towards,
                "route_back": route_back,
                "snr_back": snr_back,
            },
            from_node=frm,
            to_node=replay_build.BROADCAST,
        )
    ]
    return get_replay_manager().inject(session_id, pkts, channel=channel)


@app.tool()
def replay_inject_waypoint(
    session_id: str,
    lat: float,
    lon: float,
    *,
    name: str = "",
    description: str = "",
    icon: int = 0,
    geofence_radius: int = 0,
    bbox: list[float] | None = None,
    notify_on_enter: bool = False,
    notify_on_exit: bool = False,
    notify_favorites_only: bool = False,
    from_node: int | None = None,
    channel: str = "LongFast",
    count: int = 1,
) -> dict[str, Any]:
    """Inject a WAYPOINT_APP packet (portnum 8) into a running replay session.

    Convenience wrapper with full geofence support — populates
    `geofence_radius`, `bounding_box`, `notify_on_enter`, `notify_on_exit`,
    and `notify_favorites_only` so the Apple app's enter/exit alert flow can
    be exercised without real hardware.

    `bbox` is `[south, west, north, east]` in decimal degrees. `geofence_radius`
    is a circular radius in metres. Either or both may be set simultaneously.

    Follow up with `replay_inject(sid, "position", {"lat":…,"lon":…},
    from_node=<tracker>)` to drive a synthetic node's position through the
    geofence boundary and trigger the client's enter/exit notifications.
    """
    frm = from_node if from_node is not None else 0x0A1B2C3D
    args: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "name": name,
        "description": description,
        "icon": icon,
        "geofence_radius": geofence_radius,
        "notify_on_enter": notify_on_enter,
        "notify_on_exit": notify_on_exit,
        "notify_favorites_only": notify_favorites_only,
    }
    if bbox is not None:
        args["bbox"] = bbox
    pkts = [
        replay_build.from_kind("waypoint", args, from_node=frm, to_node=replay_build.BROADCAST)
        for _ in range(max(1, count))
    ]
    return get_replay_manager().inject(session_id, pkts, channel=channel)


@app.tool()
def replay_fuzz_presets() -> dict[str, Any]:
    """List the built-in replay fuzz presets and the fault categories each enables.

    Use the returned names as the `fuzz=` argument to `replay_start`, or pass a
    dict like `{"preset": "parser", "drop": 0.1}` to override individual rates.
    """
    # campaign tuning params only matter when their controlling flag is on
    gated = {
        "evil_twin_interval": "evil_twin",
        "flooder_rate": "flooder",
        "gps_spoofer_interval": "gps_spoofer",
        "forged_acks_interval": "forged_acks",
        "rogue_admin_interval": "rogue_admin",
        "waypoint_spam_interval": "waypoint_spam",
        "ninja_flood_interval": "ninja_flood",
        "ninja_flood_batch": "ninja_flood",
    }
    out = {}
    for nm in replay_fuzz.PRESET_NAMES:
        cfg = replay_fuzz.preset(nm)
        enabled = {}
        for k in cfg.__dataclass_fields__:
            if k in ("seed", "name"):
                continue
            v = getattr(cfg, k)
            gate = gated.get(k)
            if v and not (gate and not getattr(cfg, gate)):
                enabled[k] = v
        out[nm] = enabled
    return {
        "presets": out,
        "usage": "replay_start(fuzz='parser') or fuzz={'preset':'adversary','flooder_rate':20}",
    }


@app.tool()
def replay_status(session_id: str | None = None) -> dict[str, Any]:
    """Status of replay session(s): connection state, packets_sent/total, mode.

    Pass a `session_id` for one session, or omit for all running sessions.
    """
    return get_replay_manager().status(session_id)


@app.tool()
def replay_stop(session_id: str | None = None) -> dict[str, Any]:
    """Stop a replay session (or all sessions if `session_id` is omitted)."""
    return get_replay_manager().stop(session_id)


def _parse_iso_epoch(s: str) -> int:
    from datetime import datetime

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


# ---------- Fixture / test-data push --------------------------------------


@firmware_tool()
def push_fake_nodedb(
    size: int,
    target: str = "portduino",
    port: str | None = None,
    portduino_config: str = "default",
    backup_existing: bool = True,
    confirm: bool = False,
    reboot_after: bool = True,
    custom_seed_jsonl: str | None = None,
) -> dict[str, Any]:
    """Push a fake-NodeDB v25 fixture (250/500/1000/2000 nodes) onto a device.

    Two transports:
      target="portduino" — file copy to ~/.portduino/<portduino_config>/prefs/nodes.proto.
                            Fast, no device connection needed.
      target="hardware"  — XModem upload over serial/BLE to /prefs/nodes.proto.
                            Requires `port` + `confirm=True`. Triggers a reboot
                            so loadFromDisk picks up the new file at next boot.

    Compiles a fresh-timestamp proto from the committed JSONL seed under
    test/fixtures/nodedb/seed_v25_<N>.jsonl each invocation, so the loaded
    NodeDB always looks "recent" to the connecting phone. Structural data
    (names, IDs, positions, telemetries) is deterministic per the seed.

    Override the JSONL via `custom_seed_jsonl` to push a hand-edited scenario.
    """
    return fixtures.push_fake_nodedb(
        size=size,
        target=target,  # type: ignore[arg-type]
        port=port,
        portduino_config=portduino_config,
        backup_existing=backup_existing,
        confirm=confirm,
        reboot_after=reboot_after,
        custom_seed_jsonl=custom_seed_jsonl,
    )


# ---------- MCP tool annotations (modern hint metadata) -------------------
# Annotations let clients reason about each tool without calling it: surface
# read-only vs destructive in the UI, auto-approve safe reads, warn before
# device-mutating actions, etc. These COMPLEMENT (not replace) the `confirm=`
# gate on destructive tools. Applied post-registration so we don't decorate 53
# call sites by hand. See https://modelcontextprotocol.io (Tool annotations).

# Read-only: observe without changing device/host state.
_READ_ONLY = {
    "doctor",
    "get_environment_doctor_report",
    "get_active_capabilities",
    "android_docs_search",
    "android_docs_fetch",
    "android_version_lookup",
    "android_render_compose_preview",
    "triage_bundle",
    "list_devices",
    "list_boards",
    "get_board",
    "build_poll",  # reads background-build state; never mutates
    "flash_poll",  # reads background-flash state; never mutates
    "serial_list",
    "serial_read",  # reads buffered bytes; no write side-effect
    "device_info",
    "list_nodes",
    "get_config",
    "get_channel_url",
    "config_snapshots_list",  # lists saved snapshots; no device/host mutation
    "config_diff",  # reads snapshots/live config; no mutation
    "uhubctl_list",
    "esptool_chip_info",
    "picotool_info",
    "userprefs_manifest",
    "userprefs_get",
    "logs_window",
    "telemetry_timeline",
    "packets_window",
    "events_window",
    "recorder_status",
    "replay_status",  # reads replay-session run-state; never mutates
    "replay_fuzz_presets",  # static catalog of fuzz presets; no state
    "capture_screen",
    "summarize_window",  # reads a recorder window, distills via local model; no mutation
    "vision_oracle",  # reads a screenshot, asks the local model; no mutation
    "triage_window",  # reads device window (+optional screenshot); no mutation
    "local_model_status",  # reports backend/reachability; no mutation
    "rf_scan",  # passive SDR capture; no device/host mutation
    "pa_meter_status",  # reads the power meter (version/stored freq/live dBm); no state change
    "sdk_status",  # reports SDK-CLI bridge availability; no mutation
    "sdk_device_info",  # reads device snapshot via the Kotlin SDK CLI; no mutation
    "sdk_list_nodes",  # reads the device node DB via the Kotlin SDK CLI; no mutation
}
# Destructive: irreversible or device-state-mutating (most are confirm-gated too).
_DESTRUCTIVE = {
    "build_start",  # launches a pio subprocess; cannot be undone mid-flight
    "flash_start",  # launches a pio upload subprocess
    "build",
    "clean",
    "pio_flash",
    "erase_and_flash",
    "update_flash",
    "touch_1200bps",
    "set_owner",
    "set_config",
    "set_channel_url",
    "set_debug_log_api",
    "send_text",  # injects a mesh packet; cannot be recalled
    "inject_frame",  # injects a forged frame into the RX pipeline; cannot be recalled
    "rf_confirm_tx",  # calls send_text internally; injects a mesh packet
    # Selects the meter's active calibration curve (set_freq_mhz) — a transient,
    # non-persisted instrument state change, so not read-only. Idempotent (same
    # band -> same curve) and harmless to any DUT; the set_config-style pattern.
    "pa_measure",
    "pa_sweep",  # writes lora.tx_power, keys TX, may reboot the node
    "send_input_event",  # drives device button/GPIO; side-effect on hardware
    "reboot",
    "shutdown",
    "factory_reset",
    "mark_event",  # writes to events.jsonl
    "recorder_pause",  # mutates recorder run-state
    "recorder_resume",  # mutates recorder run-state
    "serial_open",  # acquires the exclusive port lock
    "serial_close",  # releases the port lock / tears down session
    "userprefs_set",
    "userprefs_reset",
    "userprefs_testing_profile",
    "uhubctl_power",
    "uhubctl_cycle",
    "esptool_erase_flash",
    "esptool_raw",
    "nrfutil_dfu",
    "nrfutil_raw",
    "picotool_load",
    "picotool_raw",
    "push_fake_nodedb",
    # Writes/overwrites <stream>.jsonl under a client-supplied dest_dir on the
    # host filesystem — surface it so clients don't treat it as a benign read.
    "recorder_export",
    # Writes a snapshot JSON to the host filesystem (and reads the device).
    "config_snapshot",
    # Bind a TCP listener and serve a simulated mesh to a connecting app.
    "replay_start",
    "replay_stop",
    "replay_inject",  # emits packets onto the live connection
    "replay_inject_beacon",  # emits a MESH_BEACON_APP packet
    "replay_inject_fileinfo",  # emits a FileInfo FromRadio onto the live connection
    "replay_inject_traceroute",  # emits a TRACEROUTE_APP RouteDiscovery packet
    "replay_inject_waypoint",  # emits a WAYPOINT_APP packet (with optional geofence)
    "local_model_serve",  # spawns a detached llama-server process (and may install it)
    "local_model_serve_stop",  # terminates the managed llama-server process
    "sdk_send_text",  # injects a mesh packet via the Kotlin SDK CLI; cannot be recalled
}
# Idempotent writes: calling with identical args leaves the device in identical
# state. Clients can safely retry these on timeout without side-effects.
_IDEMPOTENT_WRITES = {
    "set_config",
    "set_owner",
    "set_channel_url",
    "userprefs_set",
    "pa_measure",  # re-selecting the same band leaves the meter in the same state
}
# Open-world: interacts with an external device, radio mesh, or host hardware.
# Also includes tools whose OUTPUT may contain untrusted content sourced from
# remote mesh nodes (e.g. packet payloads, log lines) — relevant to the
# lethal-trifecta prompt-injection risk. See SECURITY.md.
_OPEN_WORLD = {
    "android_docs_search",  # queries the live Android Knowledge Base
    "android_docs_fetch",
    "android_version_lookup",  # queries live maven/Android version metadata
    "list_devices",
    "device_info",
    "list_nodes",
    "get_config",
    "set_config",
    "get_channel_url",
    "set_channel_url",
    "config_snapshot",  # reads live device config
    "config_diff",  # may read live device config (name_b=None)
    "set_owner",
    "set_debug_log_api",
    "send_text",
    "inject_frame",
    "reboot",
    "shutdown",
    "factory_reset",
    "send_input_event",
    "capture_screen",
    "touch_1200bps",
    "pio_flash",
    "flash_start",
    "erase_and_flash",
    "update_flash",
    "serial_open",
    "serial_read",
    "uhubctl_list",
    "uhubctl_power",
    "uhubctl_cycle",
    "esptool_chip_info",
    "esptool_erase_flash",
    "esptool_raw",
    "nrfutil_dfu",
    "nrfutil_raw",
    "picotool_info",
    "picotool_load",
    "picotool_raw",
    "push_fake_nodedb",
    # Talk to the external ImmersionRC power meter over USB; pa_sweep also drives
    # the node's TX onto the mesh.
    "pa_meter_status",
    "pa_measure",
    "pa_sweep",
    # Return user-authored content from remote mesh nodes — untrusted input
    # that can carry prompt-injection payloads (lethal-trifecta leg 2).
    "logs_window",
    "packets_window",
}


# Display-name overrides for tools where name.replace("_", " ").title() is poor.
_TITLE_OVERRIDES: dict[str, str] = {
    "build_start": "Build Firmware (Async Start)",
    "build_poll": "Build Firmware (Poll Status)",
    "flash_start": "Flash Firmware (Async Start)",
    "flash_poll": "Flash Firmware (Poll Status)",
    "pio_flash": "PlatformIO Flash",
    "erase_and_flash": "Erase and Factory Flash (ESP32)",
    "update_flash": "OTA App-Partition Flash (ESP32)",
    "esptool_chip_info": "ESPTool Chip Info",
    "esptool_erase_flash": "ESPTool Erase Flash",
    "esptool_raw": "ESPTool Raw Pass-Through",
    "nrfutil_dfu": "nRF DFU Flash",
    "nrfutil_raw": "nRFUtil Raw Pass-Through",
    "picotool_info": "Picotool Info",
    "picotool_load": "Picotool Load UF2",
    "picotool_raw": "Picotool Raw Pass-Through",
    "uhubctl_list": "USB Hub List",
    "uhubctl_power": "USB Hub Port Power",
    "uhubctl_cycle": "USB Hub Port Cycle",
    "get_environment_doctor_report": "Environment Doctor Report",
    "get_active_capabilities": "Active Capabilities",
    "pa_meter_status": "PA Meter Status",
    "pa_measure": "PA Meter Measure",
    "pa_sweep": "PA Power Sweep (Closed-Loop)",
}


def _apply_tool_annotations() -> None:
    """Apply MCP hint metadata to every registered tool.

    Reaches into FastMCP's private `_tool_manager._tools`. If that path ever
    changes we want a loud failure — NOT a silent swallow that leaves every
    tool with worst-case defaults (destructive/open-world/non-idempotent).
    """
    try:
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        log.warning("mcp.types.ToolAnnotations not available — skipping: %s", exc)
        return

    try:
        tools = app._tool_manager._tools
    except AttributeError as exc:
        # FastMCP private API changed. Fail loudly — unprotected tools are a
        # security regression that must be fixed, not logged and forgotten.
        log.error(
            "_apply_tool_annotations: FastMCP private API changed (%s). "
            "All tools lack annotations and will be treated as destructive/open-world "
            "by MCP clients. Fix this before deployment.",
            exc,
        )
        raise

    for name, tool in tools.items():
        read_only = name in _READ_ONLY
        title = _TITLE_OVERRIDES.get(name) or name.replace("_", " ").title()
        tool.annotations = ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=name in _DESTRUCTIVE,
            idempotentHint=read_only or name in _IDEMPOTENT_WRITES,
            openWorldHint=name in _OPEN_WORLD,
        )


_apply_tool_annotations()


# ---------- MCP resources (readable context, no tool round-trip) -----------
@app.resource(
    "meshtastic://doctor",
    name="Environment doctor report",
    mime_type="application/json",
)
def _resource_doctor() -> str:
    """Live capability + dependency report (same data as the `doctor` tool)."""
    import json

    return json.dumps(doctor_mod.run().to_dict(), indent=2)


@app.resource(
    "meshtastic://capabilities",
    name="Active capabilities",
    mime_type="text/plain",
)
def _resource_capabilities() -> str:
    """One-line summary of which capability groups are active on this host."""
    return capabilities.detect().summary()


@app.resource(
    "meshtastic://device/info",
    name="Live device info",
    mime_type="application/json",
)
def _resource_device_info() -> str:
    """Live device summary (firmware/region/node id) for the auto-selected port.

    Reads the single connected device. For multi-device hosts use the
    `device_info` tool with an explicit port instead.
    """
    import json

    try:
        return json.dumps(info.device_info(port=None), indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://device/nodes",
    name="Live mesh node database",
    mime_type="application/json",
)
def _resource_device_nodes() -> str:
    """Live node DB (local node + peers) for the auto-selected port."""
    import json

    try:
        return json.dumps(info.list_nodes(port=None), indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://e2e/{loop}",
    name="e2e loop recipe",
    mime_type="text/markdown",
)
def _resource_e2e_loop(loop: str) -> str:
    """A bundled meshtastic-e2e reference doc (e.g. loop-inbound, harness, topology)."""
    import pathlib

    base = pathlib.Path(__file__).parent / "skills" / "meshtastic-e2e" / "references"
    name = loop if loop.endswith(".md") else f"{loop}.md"
    path = (base / name).resolve()
    if not path.is_file() or base.resolve() not in path.parents:
        avail = sorted(p.stem for p in base.glob("*.md"))
        return f"unknown loop {loop!r}. available: {', '.join(avail)}"
    return path.read_text(encoding="utf-8")


# ---------- MCP prompts (pre-baked agent workflows) ------------------------
@app.prompt(title="Triage an e2e failure")
def triage_e2e_failure(token: str = "", deadline_iso: str = "") -> str:
    """Guide a dual-plane root-cause analysis of a failed e2e loop."""
    return (
        "An e2e loop FAILED. Find the root cause by correlating the two planes:\n"
        f"- token: {token or '<the unique marker token>'}\n"
        f"- deadline: {deadline_iso or '<wall-clock of the assertion>'}\n\n"
        "1. Device plane (source of truth): call `packets_window` and `logs_window` for the\n"
        "   window around the deadline. Did a TEXT_MESSAGE_APP carrying the token actually\n"
        "   reach the wire? Check for NAK/err=5 (MAX_RETRANSMIT) on broadcasts.\n"
        "2. App plane: inspect the app `layout`/`screen capture` at the deadline. Was the app\n"
        "   connected (Subscribed)? On the right screen? Was the token rendered but missed?\n"
        "3. Align by shared epoch timestamps. Classify: never-sent / sent-not-received /\n"
        "   received-not-rendered / rendered-not-asserted.\n"
        "4. Emit a one-line root-cause hypothesis + the minimal repro. See meshtastic://e2e/harness."
    )


@app.prompt(title="Bring up a device")
def bringup_device(port: str = "", region: str = "US") -> str:
    """Connect to a Meshtastic device and verify a healthy baseline."""
    return (
        f"Bring up the Meshtastic device{' at ' + port if port else ''}:\n"
        "1. `list_devices` to find it (or use the given port / a tcp://host:port).\n"
        "2. `device_info` for firmware version + node identity; `list_nodes` for the DB.\n"
        f"3. Confirm region is set ({region}); if unset, `get_config('lora')` then `set_config`.\n"
        "4. `send_text` a marker to ^all and confirm it leaves on the wire.\n"
        "Report firmware version, region, node count, and any config that looks off."
    )


@app.prompt(title="Run the inbound e2e loop")
def inbound_loop() -> str:
    """Drive the device->app inbound loop and report a verdict."""
    return (
        "Run the inbound (device->app) e2e loop. Read meshtastic://e2e/loop-inbound and\n"
        "meshtastic://e2e/harness first. Bring up the virtual mesh, connect the app over TCP,\n"
        "broadcast a unique marker token from the tester, and assert it renders in the app.\n"
        "Prefer a journey (meshtastic://e2e/journeys) over hardcoded taps. Respect the hard\n"
        "rules: unique token, bounded polling, the recorder is the device-side oracle. Emit\n"
        "`LOOP inbound PASS|FAIL token=... latency=...`."
    )


@app.prompt(title="Compare two firmware versions")
def compare_firmware(ref_a: str = "", ref_b: str = "") -> str:
    """Assess the app-facing behavioral delta between two firmware refs (impact agent)."""
    return (
        f"Compare firmware {ref_a or '<ref A / baseline>'} vs {ref_b or '<ref B / candidate>'} "
        "for app-facing impact:\n"
        "1. Build each: `scripts/build_meshtasticd.sh --env native-macos --ref <ref>` (records\n"
        "   the resolved sha). Or use the CI `firmware_ref` input.\n"
        "2. For each build, run the inbound + outbound + node-sync loops against the SAME app\n"
        "   build (pin it), capturing the device-plane recorder windows + the app verdicts.\n"
        "3. Diff: protobuf/config changes, new/renamed packet fields, timing/latency, any loop\n"
        "   that flips PASS<->FAIL. Use `android_docs_search` for migration notes where relevant.\n"
        "4. Report: a per-loop verdict table (A vs B) + a short app-facing-impact summary and the\n"
        "   two resolved shas. Flag anything that would break the current app."
    )
