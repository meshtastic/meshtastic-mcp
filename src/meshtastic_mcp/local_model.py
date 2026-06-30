# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Optional local-model offload (Ollama).

A thin, dependency-free client for a local Ollama instance. Used to push
**bulk, mechanical** sub-tasks off the agent's context window and onto a local
GPU — summarizing recorder windows, triaging logs, narrating packet streams,
and (with a multimodal model) the vision oracle. Keep *reasoning, decisions, and
final verdicts* with the calling agent; treat local output as an untrusted draft.

Disabled by default: tools that use this only register when a backend is reachable
(``has_local_model``). Two backends are supported — **Ollama** (default) or any
**OpenAI-compatible** server (e.g. a self-contained ``llama-server``; see
``llama_server.py`` for the Gemma 4 one-liner). The same offload + vision tools
work against either. Configure via env:

  MESHTASTIC_MCP_LOCAL_BACKEND ``ollama`` (default) | ``openai``
  MESHTASTIC_MCP_OLLAMA_HOST   ollama base URL (default http://127.0.0.1:11434)
  MESHTASTIC_MCP_LOCAL_BASE_URL openai base URL (default http://127.0.0.1:8080/v1)
  MESHTASTIC_MCP_LOCAL_MODEL   accuracy model (default gemma4:e4b)
  MESHTASTIC_MCP_LOCAL_FAST    fast model     (default gemma4:e2b)
  MESHTASTIC_MCP_LOCAL_VISION  vision model   (default gemma4:e2b)

All lanes default to Gemma 4 (multimodal + reasoning, one model for text and
vision); override per lane via env. Gemma 4 emits a `thinking` channel, so the
client suppresses it (``think=false``) for direct answers.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


def backend() -> str:
    """Active backend: ``ollama`` (native API) or ``openai`` (OpenAI-compatible)."""
    return os.environ.get("MESHTASTIC_MCP_LOCAL_BACKEND", "ollama").strip().lower()


def host() -> str:
    return os.environ.get("MESHTASTIC_MCP_OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def base_url() -> str:
    """OpenAI-compatible base URL (used when backend is ``openai``)."""
    return os.environ.get("MESHTASTIC_MCP_LOCAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def model(lane: str = "default") -> str:
    """Resolve a model by lane: ``default`` (accuracy), ``fast``, or ``vision``."""
    env = {
        "default": "MESHTASTIC_MCP_LOCAL_MODEL",
        "fast": "MESHTASTIC_MCP_LOCAL_FAST",
        "vision": "MESHTASTIC_MCP_LOCAL_VISION",
    }.get(lane, "MESHTASTIC_MCP_LOCAL_MODEL")
    fallback = {
        "default": "gemma4:e4b",  # accuracy: triage / structured (~3.7 GB, GPU-resident)
        "fast": "gemma4:e2b",  # bulk summarize (~2.1 GB, GPU-resident)
        "vision": "gemma4:e2b",  # multimodal oracle
    }[lane if lane in ("default", "fast", "vision") else "default"]
    return os.environ.get(env, fallback)


def available(*, timeout: float = 2.0) -> bool:
    """True when the active backend answers its model-list endpoint."""
    url = f"{base_url()}/models" if backend() == "openai" else f"{host()}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def list_models(*, timeout: float = 4.0) -> list[str]:
    try:
        if backend() == "openai":
            with urllib.request.urlopen(f"{base_url()}/models", timeout=timeout) as r:
                data = json.loads(r.read())
            return [m["id"] for m in data.get("data", [])]
        with urllib.request.urlopen(f"{host()}/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return []


def complete(
    prompt: str,
    *,
    system: str | None = None,
    lane: str = "default",
    model_name: str | None = None,
    images: list[str] | None = None,
    think: bool = False,
    timeout: float = 120.0,
    num_predict: int = 512,
) -> str:
    """One-shot chat completion against the local model. Returns the text.

    ``images`` is a list of base64-encoded PNGs (for the vision lane). ``think``
    is False by default so reasoning models (e.g. gemma4) return a direct answer
    in ``content`` instead of spending the token budget in a ``thinking`` channel.
    Raises ``LocalModelError`` on transport failure so callers degrade gracefully.
    Routes to the Ollama native API or an OpenAI-compatible server per ``backend()``.
    """
    name = model_name or model(lane)
    if backend() == "openai":
        return _complete_openai(prompt, system, name, images, think, timeout, num_predict)
    return _complete_ollama(prompt, system, name, images, think, timeout, num_predict)


def _post_json(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LocalModelError(f"local model call failed: {exc}") from exc


def _complete_ollama(prompt, system, name, images, think, timeout, num_predict):  # type: ignore[no-untyped-def]
    msg: dict = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    body = {
        "model": name,
        "messages": ([{"role": "system", "content": system}] if system else []) + [msg],
        "stream": False,
        "think": think,
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }
    data = _post_json(f"{host()}/api/chat", body, timeout)
    m = data.get("message") or {}
    # prefer the direct answer; fall back to the reasoning channel if that's all
    # a thinking model produced within the token budget
    return (m.get("content") or m.get("thinking") or "").strip()


def _complete_openai(prompt, system, name, images, think, timeout, num_predict):  # type: ignore[no-untyped-def]
    # OpenAI-compatible (e.g. llama-server). For vision, send multi-part content
    # with the image *before* the text, per Gemma 4 modality-order guidance.
    if images:
        content: object = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}}
            for b in images
        ] + [{"type": "text", "text": prompt}]
    else:
        content = prompt
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": content}
    ]
    body = {
        "model": name,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": num_predict,
    }
    if not think:
        # standard knob to suppress a reasoning channel (honored by Ollama's /v1 and
        # ignored by servers that don't reason) so the budget yields a direct answer
        body["reasoning_effort"] = "none"
    data = _post_json(f"{base_url()}/chat/completions", body, timeout)
    choice = (data.get("choices") or [{}])[0].get("message") or {}
    return (
        choice.get("content") or choice.get("reasoning_content") or choice.get("reasoning") or ""
    ).strip()


def encode_image(path: str) -> str:
    """Base64-encode an image file for the vision lane."""
    import base64

    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def vision_assert(
    image_path: str, question: str, *, model_name: str | None = None, timeout: float = 180.0
) -> dict:
    """Ask a **local** vision model a yes/no assertion about a screenshot.

    The offline vision oracle: keeps app screenshots on the box. Returns
    ``{match: bool, answer: "yes|no|unclear", evidence: str, model}``. Phrase
    ``question`` as a yes/no, e.g. "Does a message bubble containing 'E2E-1782'
    appear?". Treat as an untrusted draft — it's a fallback for when the a11y
    tree is empty (WebView/Canvas/map), not a replacement for exact-match.
    """
    system = (
        "You are a UI test oracle. Look at the screenshot and answer the yes/no "
        "question. Reply on two lines:\nANSWER: yes|no|unclear\nEVIDENCE: <what you "
        "see that justifies it, quoting any relevant on-screen text>."
    )
    raw = complete(
        question,
        system=system,
        lane="vision",
        model_name=model_name,
        images=[encode_image(image_path)],
        timeout=timeout,
        num_predict=200,
    )
    answer, evidence = "unclear", raw.strip()
    for line in raw.splitlines():
        low = line.strip().lower()
        if low.startswith("answer:"):
            v = low.split(":", 1)[1].strip()
            answer = "yes" if v.startswith("y") else "no" if v.startswith("n") else "unclear"
        elif low.startswith("evidence:"):
            evidence = line.split(":", 1)[1].strip()
    return {
        "match": answer == "yes",
        "answer": answer,
        "evidence": evidence,
        "model": model_name or model("vision"),
    }


class LocalModelError(RuntimeError):
    """Raised on a local-model transport/availability failure."""
