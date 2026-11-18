# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local-model tools: offload to a local model, and bring its backend up."""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

from .. import log_query
from ._app import local_serve_tool, local_tool
from .core import triage_bundle


@local_tool()
def summarize_window(
    start: str = "-5m",
    end: str = "now",
    focus: str = "anomalies and errors",
    lane: str = "default",
) -> dict[str, Any]:
    """Distill a recorder window into a short summary via a **local** model (offload).

    Sends the window's logs, packets and events to the local model instead of the agent's
    context and asks for a terse summary. The summary is an untrusted draft: verify it against
    the raw windows before acting on anything correctness-critical. See docs/local-models.md.

    Returns `{summary, model, counts}`.

    Args:
        start: Window start, e.g. `-5m`, `now`, or epoch/ISO.
        end: Window end, in the same forms.
        focus: What to focus the summary on, e.g. "errors", "reboots", "who talked to whom".
        lane: Model lane, `fast` or `default`.
    """
    import json

    from .. import local_model

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
        raise ToolError(str(exc)) from exc
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

    The `vision-oracle.md` fallback for when the a11y tree is empty (WebView, Canvas, map,
    mid-animation): ask a yes/no question, e.g. `vision_oracle("/tmp/shot.png", "Does a
    message bubble containing 'E2E-1782' appear?")`. The image never leaves the host.
    A draft oracle: prefer the a11y tree when it works. Needs a multimodal local model
    (`MESHTASTIC_MCP_LOCAL_VISION`).

    Returns `{match: bool, answer, evidence, model}`.

    Args:
        image_path: Path to the screenshot image on this host.
        question: A yes/no question about the screenshot.
    """
    from .. import local_model

    try:
        return local_model.vision_assert(image_path, question)
    except (local_model.LocalModelError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@local_tool()
def triage_window(
    start: str = "-2m", end: str = "now", token: str = "", screenshot: str = ""
) -> dict[str, Any]:
    """First-pass e2e-failure triage via a **local** model (offload the correlation).

    Asks the local model to put the device-plane window (logs, packets, events) in one
    bucket, `never_sent`, `sent_not_received`, `received_not_rendered` or
    `rendered_not_asserted`, with one line of evidence, as `triage.md` does. **The agent owns
    the PASS/FAIL verdict**: this is a draft hypothesis to confirm against the raw artifacts.

    Returns `{bucket, reasoning, vision, counts}`.

    Args:
        start: Window start, e.g. `-2m`, `now`, or epoch/ISO.
        end: Window end, in the same forms.
        token: Marker token that scopes the triage, such as the message the test sent.
        screenshot: Path to an app screenshot; folds in a local vision read of the app.
    """
    import json

    from .. import local_model

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
        raise ToolError(str(exc)) from exc
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

    Shows the active backend (`ollama` or `openai`), its URL, whether it
    answers, the model lanes in use, and, if a llama.cpp server is managed here,
    its pid/url. Registered when a backend is reachable or a `llama` binary exists.

    Returns `{backend, url, reachable, models, lanes, llama_server}`.
    """
    from .. import llama_server, local_model

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

    Runs `llama serve -hf <model_ref> --port <port>` detached, waits for its
    OpenAI-compatible `/v1`, and points this process at it (backend=openai). The offload
    tools register at startup, so to get them against a fresh llama-server, set the env and
    start it before the MCP server. See docs/local-models.md.

    Returns `{ok, url, log, pid, port, model}`, or `{ok: false, error}` with `stage` set to
    `install` when the install failed.

    Args:
        model_ref: Hugging Face GGUF repo to serve.
        port: Local port for the server.
        install: Set `install=True` to fetch the llama.cpp binary first (networked).
    """
    import os

    from .. import llama_server

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
    """Stop the llama.cpp server started by `local_model_serve` (if any).

    Returns `{ok, stopped, pid}`, or `{ok, stopped: false, reason}` when none is tracked.
    """
    from .. import llama_server

    return llama_server.stop()
