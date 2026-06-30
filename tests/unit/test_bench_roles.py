"""The bench board registry (tests/_bench.py) + location-aware role resolution.

The whole point of the registry is that three nRF52 boards share VID 0x239a and
MUST be told apart by hub-slot location. These tests lock that invariant in and
prove resolve_port_by_role binds to the SPECIFIC board on a role's slot rather
than the first same-VID device it sees.
"""

from __future__ import annotations

import pytest

from tests import _bench, _port_discovery


def test_four_distinct_roles_with_distinct_locations_and_envs():
    roles = _bench.roles()
    assert len(roles) == 4
    # Each role maps to a distinct hub slot and a distinct firmware env.
    locations = [_bench.role_location(r) for r in roles]
    envs = [_bench.role_env(r) for r in roles]
    assert len(set(locations)) == 4, f"locations not unique: {locations}"
    assert len(set(envs)) == 4, f"envs not unique: {envs}"
    assert all(locations) and all(envs)


def test_three_nrf52_share_vid_but_split_by_location():
    """The crux: t_echo / heltec_t114 / rak4631 all carry 0x239a, so VID can't
    separate them — only location can."""
    nrf52_roles = [r for r in _bench.roles() if 0x239A in _bench.role_vids(r)]
    assert set(nrf52_roles) == {"t_echo", "heltec_t114", "rak4631"}
    locs = {r: _bench.role_location(r) for r in nrf52_roles}
    assert len(set(locs.values())) == 3, f"same-VID boards share a slot: {locs}"


def test_esp32s3_accepts_native_and_cp2102_vids():
    vids = _bench.role_vids("esp32s3")
    assert 0x10C4 in vids  # CP2102 (Heltec V3)
    assert 0x303A in vids  # native ESP32-S3


def test_role_envs_match_registry():
    assert _bench.role_envs() == {
        "t_echo": "t-echo-plus",
        "heltec_t114": "heltec-mesh-node-t114",
        "esp32s3": "heltec-v3",
        "rak4631": "rak4631",
    }


@pytest.mark.parametrize(
    "location,expected",
    [
        ("20-3.1", ("20-3", 1)),
        ("20-3.7", ("20-3", 7)),
        ("1-1.3.2", ("1-1.3", 2)),
        (None, None),
        ("", None),
        ("no-dot", None),
        ("20-3.x", None),
    ],
)
def test_location_hub_port_parsing(location, expected):
    assert _bench.location_hub_port(location) == expected


def test_hub_profile_shape_carries_location_and_env():
    prof = _bench.hub_profile()
    assert set(prof) == set(_bench.roles())
    for role, spec in prof.items():
        assert spec["vid"] == _bench.BENCH_ROLES[role]["vid"]
        assert spec["location"] == _bench.role_location(role)
        assert spec["env"] == _bench.role_env(role)
        assert spec["pid_contains"] is None


def test_device_location_uses_hub_slot(monkeypatch):
    monkeypatch.setattr(
        "meshtastic_mcp.port_recovery.hub_slot_for_port",
        lambda port: ("20-3", 7) if port == "/dev/cu.rak" else (None, None),
    )
    assert _bench.device_location("/dev/cu.rak") == "20-3.7"
    assert _bench.device_location("/dev/cu.unknown") is None


def test_resolve_port_by_role_binds_to_the_slot_not_first_vid(monkeypatch):
    """Three 0x239a boards on three slots — resolve_port_by_role('rak4631')
    must return the board on rak4631's slot (20-3.7), NOT the first 0x239a."""
    rows = [
        {"port": "/dev/cu.techo", "vid": "0x239a"},  # slot 1
        {"port": "/dev/cu.t114", "vid": "0x239a"},  # slot 2
        {"port": "/dev/cu.rak", "vid": "0x239a"},  # slot 7
    ]
    slot_by_port = {
        "/dev/cu.techo": "20-3.1",
        "/dev/cu.t114": "20-3.2",
        "/dev/cu.rak": "20-3.7",
    }
    monkeypatch.setattr(_port_discovery.devices_module, "list_devices", lambda **_: rows)
    monkeypatch.setattr(_bench, "device_location", lambda port: slot_by_port.get(port))
    assert _port_discovery.resolve_port_by_role("rak4631", timeout_s=2.0) == "/dev/cu.rak"
    assert _port_discovery.resolve_port_by_role("t_echo", timeout_s=2.0) == "/dev/cu.techo"


def test_resolve_port_by_role_waits_out_a_missing_slot(monkeypatch):
    """If the role's board isn't on its slot, we do NOT fall back to a
    same-VID sibling — we time out (and report the location)."""
    rows = [{"port": "/dev/cu.techo", "vid": "0x239a"}]
    monkeypatch.setattr(_port_discovery.devices_module, "list_devices", lambda **_: rows)
    monkeypatch.setattr(_bench, "device_location", lambda port: "20-3.1")
    with pytest.raises(AssertionError, match="rak4631"):
        _port_discovery.resolve_port_by_role("rak4631", timeout_s=1.0, poll_start=0.2)
