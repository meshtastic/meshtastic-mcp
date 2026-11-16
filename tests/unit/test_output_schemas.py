# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The most-used tools advertise a real outputSchema, and their real results satisfy it.

The client validates every structured result against the schema, so each case below runs a
tool through an in-memory client with realistic data from the module's own record builders.
"""

from __future__ import annotations

import asyncio

import pytest

TYPED = {
    "list_devices",
    "device_info",
    "list_nodes",
    "logs_window",
    "packets_window",
    "events_window",
    "doctor",
}


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name: str, args: dict | None = None):
    from fastmcp import Client

    async def run():
        async with Client(server.app) as client:
            return await client.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(run())


def test_typed_tools_advertise_named_properties(server) -> None:
    schemas = {t.name: t.output_schema for t in asyncio.run(server.app.list_tools())}
    for name in TYPED:
        schema = schemas[name]
        assert schema, f"{name} has no output schema"
        flat = str(schema)
        assert "'properties'" in flat, f"{name} schema names no fields: {schema}"


def test_doctor_result_satisfies_its_schema(server) -> None:
    result = _call(server, "doctor")
    assert result.is_error is False, result.content


def test_device_info_result_satisfies_its_schema(server, monkeypatch) -> None:
    monkeypatch.setattr(
        server.info,
        "device_info",
        lambda port=None, timeout_s=8.0: {
            "port": "/dev/ttyACM0",
            "my_node_num": 3735928559,
            "long_name": "Bench A",
            "short_name": "BNCA",
            "firmware_version": "2.8.4.abc123",
            "hw_model": "HELTEC_V3",
            "region": "US",
            "num_nodes": 12,
            "primary_channel": "LongFast",
            "modem_preset": "LONG_FAST",
            "channel_num": 20,
            "display": None,
        },
    )
    assert _call(server, "device_info").is_error is False


def test_list_nodes_result_satisfies_its_schema(server, monkeypatch) -> None:
    from meshtastic_mcp import info

    node = info._node_record(
        {
            "num": 1234,
            "user": {"longName": "Peer", "shortName": "PR", "hwModel": "TBEAM", "role": "CLIENT"},
            "position": {"latitude": 1.5, "longitude": 2.5, "altitude": 10, "time": 1700000000},
            "snr": 5.5,
            "rssi": -90,
            "lastHeard": 1700000001,
            "deviceMetrics": {"batteryLevel": 87},
            "isFavorite": True,
        }
    )
    bare = info._node_record({"num": 5})
    monkeypatch.setattr(server.info, "list_nodes", lambda port=None, timeout_s=8.0: [node, bare])
    result = _call(server, "list_nodes")
    assert result.is_error is False
    rows = result.structured_content["result"]
    assert rows[0]["node_num"] == 1234
    assert rows[0]["user"]["long_name"] == "Peer"
    assert rows[0]["position"]["latitude"] == 1.5
    assert len(rows) == 2


def test_list_devices_result_satisfies_its_schema(server, monkeypatch) -> None:
    from meshtastic_mcp import devices

    monkeypatch.setenv("MESHTASTIC_MCP_TCP_HOST", "127.0.0.1")
    entries = devices.list_devices(include_unknown=True)
    assert any(e["port"].startswith("tcp://") for e in entries)
    assert _call(server, "list_devices", {"include_unknown": True}).is_error is False


@pytest.mark.parametrize(
    ("tool", "rows_key"),
    [("logs_window", "lines"), ("packets_window", "packets"), ("events_window", "events")],
)
def test_window_results_satisfy_their_schemas(server, monkeypatch, tool, rows_key) -> None:
    payload = {
        rows_key: [{"ts": 1.0, "n": 1}],
        "total_matched": 1,
        "dropped": 0,
        "window": {"start": 1.0, "end": 2.0},
    }
    monkeypatch.setattr(server.log_query, tool, lambda **_: payload)
    assert _call(server, tool).is_error is False
