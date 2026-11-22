# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The default tools carry what the tools they replaced did."""

from __future__ import annotations

from meshtastic_mcp import devices as devices_mod
from meshtastic_mcp.tools import core, replay


def test_devices_lists_unrecognized_ports_only_when_asked(monkeypatch) -> None:
    radio = {"port": "/dev/ttyACM0", "likely_meshtastic": True}
    other = {"port": "/dev/ttyUSB9", "likely_meshtastic": False}

    def fake_list(include_unknown: bool = False):
        return [radio, other] if include_unknown else [radio]

    monkeypatch.setattr(devices_mod, "list_devices", fake_list)
    monkeypatch.setattr(core.device_names, "listing", lambda attached: [])
    assert "other_ports" not in core.devices()
    assert core.devices(include_unknown=True)["other_ports"] == [other]


def test_sim_status_lists_the_fuzz_presets_on_request(monkeypatch) -> None:
    monkeypatch.setattr(replay, "replay_status", lambda session_id=None: {"sessions": []})
    assert "presets" not in replay.sim_status()
    out = replay.sim_status(fuzz_presets=True)
    assert out["sessions"] == [] and "adversary" in out["presets"]
