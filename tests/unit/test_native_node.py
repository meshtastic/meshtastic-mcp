# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the native-node lab builder (no `meshtasticd` binary required)."""

from __future__ import annotations

from pathlib import Path

from meshtastic_mcp.emulator import native_node


def test_build_lab_assigns_distinct_ports_and_macs() -> None:
    nodes = native_node.build_lab(Path("/bin/true"), Path("/tmp/x"), count=3, base_port=4403)
    assert [n.tcp_port for n in nodes] == [4403, 4404, 4405]
    assert len({n.mac for n in nodes}) == 3


def test_macs_are_nonzero() -> None:
    # Regression: an all-zero MAC ("000000000000") is rejected by the firmware as "blank",
    # so node0 never bound its port and the supervisor looped forever.
    for n in native_node.build_lab(Path("/bin/true"), Path("/tmp/x"), count=2):
        assert int(n.mac, 16) != 0, f"{n.name} has a zero MAC ({n.mac!r})"
        assert len(n.mac) == 12


def test_written_config_quotes_the_mac(tmp_path: Path) -> None:
    # Regression: an unquoted MAC parses as a YAML integer (losing leading zeros / reading
    # as 0). The template must quote it.
    node = native_node.build_lab(Path("/bin/true"), tmp_path, count=1)[0]
    node._write_config()
    mac_line = next(li for li in node.config_path.read_text().splitlines() if "MACAddress" in li)
    assert f'"{node.mac}"' in mac_line
