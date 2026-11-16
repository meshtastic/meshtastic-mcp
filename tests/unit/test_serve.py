# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The HTTP serve mode refuses to run without a strong token and accepts only that token."""

from __future__ import annotations

import asyncio
import os

import pytest

from meshtastic_mcp import serve

GOOD = "x" * 40


def test_missing_token_refuses_to_start() -> None:
    with pytest.raises(serve.TokenConfigError, match="no bearer token"):
        serve.load_token({})


def test_short_token_is_rejected() -> None:
    with pytest.raises(serve.TokenConfigError, match="shorter"):
        serve.load_token({serve.TOKEN_ENV: "short"})


def test_env_token_is_loaded() -> None:
    assert serve.load_token({serve.TOKEN_ENV: f"  {GOOD}\n"}) == GOOD


def test_token_file_must_not_be_group_or_world_readable(tmp_path) -> None:
    f = tmp_path / "token"
    f.write_text(GOOD)
    os.chmod(f, 0o644)
    with pytest.raises(serve.TokenConfigError, match="chmod 600"):
        serve.load_token({serve.TOKEN_FILE_ENV: str(f)})
    os.chmod(f, 0o600)
    assert serve.load_token({serve.TOKEN_FILE_ENV: str(f)}) == GOOD


def test_verifier_accepts_only_the_configured_token() -> None:
    v = serve.BenchTokenVerifier(GOOD)
    assert asyncio.run(v.verify_token(GOOD)) is not None
    assert asyncio.run(v.verify_token(GOOD + "x")) is None
    assert asyncio.run(v.verify_token("")) is None
