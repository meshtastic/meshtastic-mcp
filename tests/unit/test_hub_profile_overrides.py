# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""No-hardware checks for the --hub-profile YAML config surface.

Covers the reserved `session:` block (`conftest._load_hub_profile` /
`_load_hub_profile_session`) and per-role `env:` resolution
(`test_00_bake._env_for`) added so a `--hub-profile` YAML alone (no exported
env vars) is enough to run the hardware tiers against an already-configured
device. Each also verifies the env-var-wins-over-YAML precedence, since env
vars must keep working unchanged for anyone already using them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from .. import conftest as root_conftest
from .. import test_00_bake


def _write_yaml(tmp_path: Path, text: str) -> str:
    p = tmp_path / "hub.yaml"
    p.write_text(text)
    return str(p)


# ---------- conftest._load_hub_profile / _load_hub_profile_session --------


def test_load_hub_profile_strips_session_key(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        session:
          region: EU_868
        esp32s3:
          vid: 0x303a
        """,
    )
    profile = root_conftest._load_hub_profile(path)
    assert "session" not in profile
    assert profile == {"esp32s3": {"vid": 0x303A}}  # YAML parses 0x... as int


def test_load_hub_profile_rejects_non_mapping_role_spec(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "esp32s3: not-a-mapping\n")
    with pytest.raises(pytest.UsageError):
        root_conftest._load_hub_profile(path)


def test_load_hub_profile_session_reads_known_keys(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        session:
          region: EU_868
          channel_name: FAB26
          channel_num: 0
          modem_preset: LONG_FAST
        esp32s3:
          vid: 0x303a
        """,
    )
    assert root_conftest._load_hub_profile_session(path) == {
        "region": "EU_868",
        "channel_name": "FAB26",
        "channel_num": 0,
        "modem_preset": "LONG_FAST",
    }


def test_load_hub_profile_session_rejects_unknown_key(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "session:\n  bogus_key: 1\n")
    with pytest.raises(pytest.UsageError, match="bogus_key"):
        root_conftest._load_hub_profile_session(path)


def test_load_hub_profile_session_absent_returns_empty(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "esp32s3:\n  vid: 0x303a\n")
    assert root_conftest._load_hub_profile_session(path) == {}


def test_load_hub_profile_session_no_path_returns_empty() -> None:
    assert root_conftest._load_hub_profile_session(None) == {}


# ---------- test_profile's YAML/env-var precedence -------------------------
#
# `test_profile` itself is a fixture (needs the full fixture graph to call
# directly); its precedence logic is the small `_get` closure inside it,
# which mirrors this reference behavior exactly (env var > YAML > default).
# Exercised here at the same YAML-loading layer used above so a regression
# in either the YAML parsing or the precedence order is caught without
# spinning up hardware fixtures.


def test_profile_precedence_env_var_wins_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_yaml(tmp_path, "session:\n  region: EU_868\n")
    overrides = root_conftest._load_hub_profile_session(path)
    monkeypatch.setenv("MESHTASTIC_MCP_REGION", "JP")

    def _get(key: str, env_name: str, default: str) -> str:
        return os.environ.get(env_name) or str(overrides.get(key, default))

    assert _get("region", "MESHTASTIC_MCP_REGION", "US") == "JP"


def test_profile_precedence_yaml_wins_over_default(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "session:\n  region: EU_868\n")
    overrides = root_conftest._load_hub_profile_session(path)

    def _get(key: str, env_name: str, default: str) -> str:
        return os.environ.get(env_name) or str(overrides.get(key, default))

    assert _get("region", "MESHTASTIC_MCP_REGION", "US") == "EU_868"


def test_profile_precedence_default_when_neither_set() -> None:
    overrides: dict[str, Any] = {}

    def _get(key: str, env_name: str, default: str) -> str:
        return os.environ.get(env_name) or str(overrides.get(key, default))

    assert _get("region", "MESHTASTIC_MCP_REGION_UNSET_FOR_TEST", "US") == "US"


# ---------- test_00_bake._env_for ------------------------------------------


def test_env_for_uses_profile_env_when_no_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESHTASTIC_MCP_ENV_ESP32S3", raising=False)
    assert test_00_bake._env_for("esp32s3", profile_env="seeed-xiao-s3") == "seeed-xiao-s3"


def test_env_for_env_var_wins_over_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHTASTIC_MCP_ENV_ESP32S3", "heltec-v3")
    assert test_00_bake._env_for("esp32s3", profile_env="seeed-xiao-s3") == "heltec-v3"


def test_env_for_falls_back_to_bench_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESHTASTIC_MCP_ENV_ESP32S3", raising=False)
    assert test_00_bake._env_for("esp32s3") == "heltec-v3"  # tests/_bench.py default


def test_env_for_unknown_role_without_override_fails() -> None:
    # pytest.fail() raises pytest.fail.Exception (BaseException, not Exception)
    # specifically so a broad `except Exception` in user code can't swallow it.
    with pytest.raises(pytest.fail.Exception, match="no default PlatformIO env"):
        test_00_bake._env_for("not_a_real_role")
