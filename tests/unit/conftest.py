# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Portable unit-tier fixtures.

Isolate the recorder data dir before any test module imports `server`/`recorder`
(server autostarts the recorder on import). Without this, the recorder's
`_default_dir()` would resolve to the platformdirs user-data dir; pinning it to a
throwaway temp dir keeps unit runs from touching real user state. `setdefault` so
an explicit override still wins.
"""

import os
import tempfile

import pytest

os.environ.setdefault("MESHTASTIC_MCP_DATA_DIR", tempfile.mkdtemp(prefix="mtmcp-unit-"))

# Per-test budget for the portable tier. Unit tests never wait on radios or
# airtime, so anything past this is a hang (a real sleep, an unmocked probe,
# an unbounded queue drain) rather than slow work. Tests that legitimately
# need longer opt in with an explicit `@pytest.mark.timeout(N)`, which wins.
# The hardware tiers keep their own per-test marks — no repo-wide default.
UNIT_TEST_TIMEOUT_S = 60


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(UNIT_TEST_TIMEOUT_S))
