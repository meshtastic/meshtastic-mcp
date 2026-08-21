# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ATAK tier: one provisioned emulator reaches the CoT relay.

The real bring-up gate the fleet tools shipped without — boot a single clone,
provision ATAK against a relay on an ephemeral port, and require a peer with a
callsign (ATAK's first PLI) within 3 minutes. Needs ``MESHTASTIC_MCP_ATAK_APK``
(auto-skipped otherwise) and an AVD named by ``MESHTASTIC_MCP_ATAK_BASE_AVD``
(default ``medium_phone``). Cold first run is ~15 min (install + first-run
walk + snapshot); subsequent runs restore the snapshot in ~1 min.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshtastic_mcp.emulator import atak, avd
from meshtastic_mcp.replay.cot_relay import CotRelay

pytestmark = pytest.mark.atak

PEER_TIMEOUT_S = 180.0


@pytest.fixture(scope="module")
def relay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CotRelay]:
    r = CotRelay(outdir=tmp_path_factory.mktemp("cot"), port=0)
    r.start()
    yield r
    r.stop()


@pytest.fixture(scope="module")
def node(relay: CotRelay) -> Iterator[atak.FleetNode]:
    apk = os.environ["MESHTASTIC_MCP_ATAK_APK"]
    base = os.environ.get("MESHTASTIC_MCP_ATAK_BASE_AVD", "medium_phone")
    if not Path(apk).is_file():
        pytest.skip(f"MESHTASTIC_MCP_ATAK_APK={apk!r} is not a file")
    if not shutil.which("adb") and avd._sdk_root() is None:
        pytest.skip("adb / Android SDK not found")
    if base not in avd.list_avds():
        pytest.skip(f"base AVD {base!r} not found (MESHTASTIC_MCP_ATAK_BASE_AVD)")
    # The relay port is ephemeral, so a restored snapshot (whose pref points at
    # the port used when it was taken) would never connect — always provision.
    fleet = atak.fleet_up(1, apk, base_avd=base, relay_port=relay.port, use_snapshot=False)
    try:
        yield fleet.nodes[0]
    finally:
        atak.fleet_down(fleet)


def test_node_connects_to_relay_with_callsign(relay: CotRelay, node: atak.FleetNode) -> None:
    deadline = time.monotonic() + PEER_TIMEOUT_S
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = relay.status()
        peers = last["peers"]
        assert isinstance(peers, list)
        if any(p["callsign"] for p in peers):
            return
        time.sleep(5)
    pytest.fail(f"no peer with a callsign on {node.serial} within {PEER_TIMEOUT_S}s: {last}")
