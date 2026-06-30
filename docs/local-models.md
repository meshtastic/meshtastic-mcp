<!--
SPDX-FileCopyrightText: Meshtastic contributors
SPDX-License-Identifier: GPL-3.0-only
-->
# Local-model offload

An optional capability that pushes token-heavy, mechanical work onto a **local** model (a local
GPU) instead of the calling agent's context window. Offload bulk summarization, triage,
classification, and extraction over text and pixels; keep reasoning, decisions, and final
pass/fail verdicts with the agent. Every local-model output is an **untrusted draft** — verify
before acting on it.

The offload tools register only when a backend is reachable (`capabilities.has_local_model()`), so
a plain install is unaffected.

## Why it fits this server

- **The recorder is a firehose** — four append-only JSONL streams (logs, packets, telemetry,
  events) are exactly the bulk text a small local model distills well, and exactly what bloats an
  agent's context if read raw.
- **E2E is dual-plane** — a failure means correlating device logs/packets against an app
  screenshot/tree, a mechanical first pass that leaves the verdict to the agent.
- **Privacy** — logs and UI screenshots never leave the box; the vision oracle runs on a local VLM.

## Tools

All register only with a reachable backend, return the model used, and degrade to `{"error": …}`
on transport failure rather than throwing.

- **`summarize_window(start, end, focus, lane)`** — distills a recorder window
  (`logs+packets+events`) into a terse bullet summary. The window JSON goes to the local GPU; the
  agent gets back a few bullets.
- **`vision_oracle(image_path, question)`** — a multimodal model answers a yes/no question about a
  screenshot, returning `{match, answer, evidence, model}`. The offline fallback for when the a11y
  tree is empty (WebView / Canvas / map).
- **`triage_window(start, end, token, screenshot)`** — first-pass e2e-failure triage: classifies
  the device window (plus an optional vision read of the app screen) into a failure bucket
  (`never_sent` / `sent_not_received` / `received_not_rendered` / `rendered_not_asserted`) with
  one-line evidence. The agent owns the final PASS/FAIL verdict.

**Never offload:** the pass/fail decision, the root-cause conclusion, any device-mutating action
(`set_config`/`flash`/`reboot`/`factory_reset`), or build/flash choices.

## Configuration

The client (`local_model.py`) is dependency-free (stdlib `urllib`) and backend-agnostic via
`MESHTASTIC_MCP_LOCAL_BACKEND`:

- **`ollama`** (default) — native API at `MESHTASTIC_MCP_OLLAMA_HOST` (default
  `http://127.0.0.1:11434`); sends `think=false`.
- **`openai`** — any OpenAI-compatible `/v1` at `MESHTASTIC_MCP_LOCAL_BASE_URL` (default
  `http://127.0.0.1:8080/v1`, e.g. `llama-server`); vision goes as `image_url` data URIs and
  `reasoning_effort=none` suppresses the thinking channel.

Model **lanes** default to **Gemma 4** (multimodal + reasoning, one model for text and vision; the
E-series use Per-Layer Embeddings, so RAM stays small), overridable per lane via env:

| Lane | Default | Env | Use | Resident |
|---|---|---|---|---|
| `default` | `gemma4:e4b` | `MESHTASTIC_MCP_LOCAL_MODEL` | triage / structured | ~3.7 GB |
| `fast` | `gemma4:e2b` | `MESHTASTIC_MCP_LOCAL_FAST` | bulk summarize | ~2.1 GB |
| `vision` | `gemma4:e2b` | `MESHTASTIC_MCP_LOCAL_VISION` | multimodal oracle | ~2.1 GB |

Both E-series sizes are 100% GPU-resident on an 8 GB card (unlike the dense `gemma4:12b`). Gemma 4
emits a `thinking` channel; the client suppresses it for a direct answer (falling back to the
reasoning text if that's all a model returned within the token budget). Swap any lane to a leaner
text model (e.g. `qwen2.5:3b-instruct`) via env for faster bulk summaries.

## Self-contained backend: the llama.cpp one-liner

Gemma 4 has image+text support in llama.cpp, so the same offload and vision tools run against a
single `llama-server` binary instead of the Ollama daemon:

```
llama serve -hf ggml-org/gemma-4-E2B-it-GGUF --port 8080   # OpenAI-compatible /v1
```

`llama_server.py` finds the binary, optionally installs it (`curl | sh`, opt-in), and
starts/stops/queries a detached server (pid tracked in a state file so it survives across tool
calls). Three tools manage it, registered when a backend is reachable *or* a `llama` binary is
present:

- **`local_model_status`** — backend, URL, reachability, model lanes, managed-server pid.
- **`local_model_serve(model_ref, port, install)`** — starts the server and points this process's
  client at it (`backend=openai`); `install=True` fetches llama.cpp first.
- **`local_model_serve_stop`** — tears it down.

The offload tools are gated at startup on a reachable backend, so to register them against a fresh
`llama-server`, set the env and start the server before launching the MCP server.
`local_model_serve` is the in-session bootstrap; after it, `local_model.*` calls route to it.
