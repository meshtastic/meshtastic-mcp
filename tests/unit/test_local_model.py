# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local-model offload (exploration). Client wiring + capability gating; the
live-completion test skips when no local Ollama is reachable."""

from __future__ import annotations

import pytest

from meshtastic_mcp import capabilities, llama_server, local_model


def test_model_lanes_and_env_override(monkeypatch):
    monkeypatch.delenv("MESHTASTIC_MCP_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("MESHTASTIC_MCP_LOCAL_FAST", raising=False)
    monkeypatch.delenv("MESHTASTIC_MCP_LOCAL_VISION", raising=False)
    assert local_model.model("default") == "gemma4:e4b"
    assert local_model.model("fast") == "gemma4:e2b"
    assert local_model.model("vision") == "gemma4:e2b"
    monkeypatch.setenv("MESHTASTIC_MCP_LOCAL_FAST", "tinyllama")
    assert local_model.model("fast") == "tinyllama"


def test_host_env_override(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_OLLAMA_HOST", "http://example:1234/")
    assert local_model.host() == "http://example:1234"


def test_backend_toggle_and_base_url(monkeypatch):
    monkeypatch.delenv("MESHTASTIC_MCP_LOCAL_BACKEND", raising=False)
    monkeypatch.delenv("MESHTASTIC_MCP_LOCAL_BASE_URL", raising=False)
    assert local_model.backend() == "ollama"  # default
    assert local_model.base_url() == "http://127.0.0.1:8080/v1"
    monkeypatch.setenv("MESHTASTIC_MCP_LOCAL_BACKEND", "OpenAI")
    monkeypatch.setenv("MESHTASTIC_MCP_LOCAL_BASE_URL", "http://host:9/v1/")
    assert local_model.backend() == "openai"  # normalized
    assert local_model.base_url() == "http://host:9/v1"


def test_openai_backend_unreachable_degrades(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_LOCAL_BACKEND", "openai")
    monkeypatch.setenv("MESHTASTIC_MCP_LOCAL_BASE_URL", "http://127.0.0.1:1/v1")
    assert local_model.available(timeout=0.5) is False
    with pytest.raises(local_model.LocalModelError):
        local_model.complete("hi", timeout=0.5)


def test_llama_server_status_and_serve_guard():
    st = llama_server.status()
    assert set(st) >= {"binary", "pid", "running", "reachable", "model", "url"}
    assert llama_server.available() == (llama_server.binary() is not None)
    if not llama_server.available():
        with pytest.raises(RuntimeError):
            llama_server.serve()


def test_capability_reflects_availability():
    # the capability mirrors reachability without raising either way
    assert capabilities.has_local_model() == local_model.available()


def test_unreachable_host_degrades(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_OLLAMA_HOST", "http://127.0.0.1:1")  # nothing listening
    assert local_model.available(timeout=0.5) is False
    with pytest.raises(local_model.LocalModelError):
        local_model.complete("hi", timeout=0.5)


def _has_vision() -> bool:
    return local_model.available() and local_model.model("vision") in local_model.list_models()


@pytest.mark.skipif(not local_model.available(), reason="no local Ollama reachable")
def test_live_completion_offloads():
    out = local_model.complete(
        "Reply with exactly the word OK and nothing else.", lane="fast", num_predict=8
    )
    assert isinstance(out, str) and out  # got a non-empty response from the local GPU


@pytest.mark.skipif(not _has_vision(), reason="no local vision model")
def test_vision_assert_parses_structured_answer(tmp_path):
    # a tiny synthetic image with known text; the VLM should read it
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    img = tmp_path / "shot.png"
    im = Image.new("RGB", (480, 160), "white")
    ImageDraw.Draw(im).text((20, 60), "GEOFENCE-TOKEN-42", fill="black")
    im.save(img)
    r = local_model.vision_assert(str(img), "Does the image contain the text 'GEOFENCE-TOKEN-42'?")
    assert set(r) >= {"match", "answer", "evidence", "model"}
    assert r["answer"] in ("yes", "no", "unclear")
