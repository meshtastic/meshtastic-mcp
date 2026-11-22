# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Typed tools advertise a real outputSchema, and their real results satisfy it.

The client validates every structured result against the schema, so each case below runs a
tool through an in-memory client with realistic data from the module's own record builders,
or with the real tool code over a faked transport (no serial port, no subprocess).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshtastic_mcp import admin, confirm, log_query, power_meter

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


def test_query_result_satisfies_its_schema(server, monkeypatch, tmp_path) -> None:
    from meshtastic_mcp.tools import core

    monkeypatch.setattr(core, "get_recorder", lambda: SimpleNamespace(base_dir=tmp_path))
    result = _call(server, "query", {"sql": "SELECT 1 AS n, now() AS t, NULL AS x"})
    assert result.is_error is False, result.content
    assert result.structured_content["rows"][0][0] == 1


def test_doctor_result_satisfies_its_schema(server) -> None:
    result = _call(server, "doctor")
    assert result.is_error is False, result.content


def test_device_info_result_satisfies_its_schema(server, monkeypatch) -> None:
    from meshtastic_mcp import info

    monkeypatch.setattr(
        info,
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
    monkeypatch.setattr(info, "list_nodes", lambda port=None, timeout_s=8.0: [node, bare])
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
    monkeypatch.setattr(log_query, tool, lambda **_: payload)
    assert _call(server, tool).is_error is False


# ---------- tools whose result shape used to live only in a docstring `Returns:` ----------


@pytest.fixture(scope="module")
def tools(server):
    from meshtastic_mcp.tools import core, firmware, hardware, rf

    return SimpleNamespace(core=core, firmware=firmware, hardware=hardware, rf=rf)


def _run_tool(fn, args: dict | None = None):
    """Call `fn` through an in-memory client, which validates the result against its schema.

    A fresh app registers it, so a tool gated on a capability this host lacks still runs.
    """
    from fastmcp import Client, FastMCP

    app = FastMCP("schema-test")
    app.tool()(fn)

    async def run():
        async with Client(app) as client:
            return await client.call_tool(fn.__name__, args or {}, raise_on_error=False)

    return asyncio.run(run())


def _typed(fn, *required: str) -> None:
    from fastmcp.tools import Tool

    schema = Tool.from_function(fn).output_schema or {}
    assert set(required) <= set(schema.get("required", [])), schema


def _ok(result) -> dict:
    assert result.is_error is False, result.content
    return result.structured_content


class _Node:
    def __init__(self) -> None:
        from meshtastic.protobuf import localonly_pb2  # type: ignore[import-untyped]

        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels: list = []
        self.sent: list = []

    def setOwner(self, long_name=None, short_name=None) -> None:
        pass

    def writeConfig(self, section) -> None:
        pass

    def setURL(self, url, addOnly=False) -> None:
        self.channels = [SimpleNamespace(role=1), SimpleNamespace(role=2), SimpleNamespace(role=0)]

    def factoryReset(self, full=False) -> None:
        pass

    def reboot(self, secs=10) -> None:
        pass

    def shutdown(self, secs=10) -> None:
        pass

    def _sendAdmin(self, msg, **_kwargs) -> None:
        self.sent.append(msg)


class _Iface:
    def __init__(self) -> None:
        self.localNode = _Node()
        self.packet_id: int | None = 1234

    def sendText(self, text, destinationId, wantAck, channelIndex):
        return SimpleNamespace(id=self.packet_id) if self.packet_id is not None else object()


@pytest.fixture
def iface(monkeypatch) -> _Iface:
    fake = _Iface()

    @contextlib.contextmanager
    def connect(port=None, **_kwargs):
        yield fake

    monkeypatch.setattr(admin, "connect", connect)
    # The intent tools and their legacy adapters reach the radio through the backends.
    from meshtastic_mcp import backends, connection
    from meshtastic_mcp.backends import python_admin

    monkeypatch.setattr(connection, "connect", connect)
    monkeypatch.setattr(python_admin.time, "sleep", lambda _s: None)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    monkeypatch.delenv(confirm.POLICY_ENV, raising=False)
    return fake


@pytest.mark.parametrize("short_name", ["BNCA", None])
def test_set_owner_result_satisfies_its_schema(tools, iface, short_name) -> None:
    _typed(tools.core.set_owner, "ok", "long_name", "short_name")
    args = {"long_name": "Bench A", **({"short_name": short_name} if short_name else {})}
    assert _ok(_run_tool(tools.core.set_owner, args))["short_name"] == short_name


@pytest.mark.parametrize(
    ("path", "value", "parent"),
    [
        ("lora.region", "US", "localConfig"),
        ("lora.tx_power", 17, "localConfig"),
        ("mqtt.enabled", True, "moduleConfig"),
    ],
)
def test_set_config_result_satisfies_its_schema(tools, iface, path, value, parent) -> None:
    _typed(tools.core.set_config, "ok", "path", "section", "parent", "old_value", "new_value")
    out = _ok(_run_tool(tools.core.set_config, {"path": path, "value": value}))
    assert out["parent"] == parent
    assert out["new_value"] == value


def test_set_channel_url_result_satisfies_its_schema(tools, iface) -> None:
    _typed(tools.core.set_channel_url, "ok", "channels_imported")
    out = _ok(_run_tool(tools.core.set_channel_url, {"url": "https://meshtastic.org/e/#x"}))
    assert out["channels_imported"] == 2


def test_config_snapshot_and_diff_results_satisfy_their_schemas(
    tools, monkeypatch, tmp_path
) -> None:
    _typed(tools.core.config_snapshot, "ok", "name", "path", "local_sections", "module_sections")
    _typed(tools.core.config_diff, "from", "to", "changed", "added", "removed", "identical")
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    wrapped = {
        "config": {
            "localConfig": {"lora": {"region": "US", "tx_power": 20}},
            "moduleConfig": {"mqtt": {"enabled": False}},
        }
    }
    # get_config's full read is wrapped in `config`; capture also takes the bare shape.
    bare = {
        "localConfig": {"lora": {"region": "EU_868"}, "device": {"role": "ROUTER"}},
        "moduleConfig": {"mqtt": {"enabled": False}},
    }
    reads = iter([wrapped, bare, bare])
    monkeypatch.setattr(admin, "get_config", lambda section=None, port=None: next(reads))

    first = _ok(_run_tool(tools.core.config_snapshot, {"name": "before"}))
    assert first["local_sections"] == ["lora"]
    _ok(_run_tool(tools.core.config_snapshot, {"name": "after"}))

    diff = _ok(_run_tool(tools.core.config_diff, {"name_a": "before", "name_b": "after"}))
    assert diff["changed"]["localConfig.lora.region"] == {"from": "US", "to": "EU_868"}
    assert "localConfig.lora.tx_power" in diff["removed"]
    assert "localConfig.device.role" in diff["added"]
    live = _ok(_run_tool(tools.core.config_diff, {"name_a": "after"}))
    assert live["to"] == "live" and live["identical"] is True


@pytest.mark.parametrize(("to", "destination"), [(None, "^all"), (1234, 1234), ("!a1", "!a1")])
def test_send_text_result_satisfies_its_schema(tools, iface, to, destination) -> None:
    _typed(tools.core.send_text, "ok", "packet_id", "destination")
    args = {"text": "hi", **({"to": to} if to is not None else {})}
    out = _ok(_run_tool(tools.core.send_text, args))
    assert out["destination"] == destination
    assert "tx_confirmed" not in out


def _logs_with_tx(**kw):
    return {"lines": [{"line": "Started Tx"}], "total_matched": 1}


def _logs_without_tx(**kw):
    return {"lines": [], "total_matched": 0} if "grep" in kw else {"lines": [{"line": "x"}]}


def _no_logs(**kw):
    return {"lines": [], "total_matched": 0}


@pytest.mark.parametrize(
    ("logs", "packet_id", "confirmed"),
    [
        (_logs_with_tx, 1234, True),
        (_logs_without_tx, 1234, False),
        (_no_logs, 1234, None),
        (_logs_with_tx, None, None),
    ],
)
def test_send_text_waiting_for_tx_satisfies_its_schema(
    tools, iface, monkeypatch, logs, packet_id, confirmed
) -> None:
    from meshtastic_mcp import log_query

    _typed(tools.core.send_text, "ok", "packet_id", "destination")
    iface.packet_id = packet_id
    monkeypatch.setattr(log_query, "logs_window", logs)
    monkeypatch.setattr(log_query, "packets_window", lambda **_: {"packets": [], "dropped": 0})
    args = {"text": "hi", "wait_for_tx": True, "tx_timeout_s": 0}
    out = _ok(_run_tool(tools.core.send_text, args))
    assert out["tx_confirmed"] is confirmed
    assert ("tx_unconfirmed_reason" in out) is (confirmed is not True)


@pytest.mark.parametrize(
    ("name", "args", "key"),
    [
        ("reboot", {"seconds": 5}, "rebooting_in_s"),
        ("shutdown", {"seconds": 5}, "shutting_down_in_s"),
        ("factory_reset", {"full": True}, "full"),
        ("factory_reset", {}, "full"),
    ],
)
def test_node_actions_satisfy_their_schemas(tools, iface, name, args, key) -> None:
    fn = getattr(tools.core, name)
    _typed(fn, "ok", key)
    out = _ok(_run_tool(fn, {"confirm": True, **args}))
    assert out["ok"] is True and key in out


@pytest.mark.parametrize(("code", "number"), [("BACK", 27), (10, 10)])
def test_send_input_event_result_satisfies_its_schema(tools, iface, code, number) -> None:
    _typed(tools.core.send_input_event, "ok", "event_code", "kb_char")
    out = _ok(_run_tool(tools.core.send_input_event, {"event_code": code, "kb_char": 104}))
    assert out["event_code"] == number


@pytest.mark.parametrize("out_path", [None, "shots/home.png"])
def test_capture_display_result_satisfies_its_schema(
    tools, monkeypatch, tmp_path, out_path
) -> None:
    from meshtastic_mcp import connection
    from meshtastic_mcp import display_mirror as dm

    from .test_display_mirror import _FakeIface

    _typed(tools.core.capture_display, "path", "bytes", "format", "blank", "panel_class")
    fake = _FakeIface()

    @contextlib.contextmanager
    def connect(port=None, **_kwargs):
        yield fake

    monkeypatch.setattr(connection, "connect", connect)
    monkeypatch.setattr(dm, "_SETTLE_S", 0.0)
    monkeypatch.setattr(dm, "_WAKE_S", 0.0)
    monkeypatch.setattr(dm, "_QUIET_S", 0.01)
    monkeypatch.setenv("MESHTASTIC_MCP_CAPTURE_DIR", str(tmp_path))
    args = {"timeout_s": 2, **({"out_path": out_path} if out_path else {})}
    out = _ok(_run_tool(tools.core.capture_display, args))
    assert out["format"] == "MONO_VLSB" and out["screen_on_secs"] == 30
    assert Path(out["path"]).is_file()


def test_flash_start_result_satisfies_its_schema(tools, monkeypatch, tmp_path) -> None:
    from meshtastic_mcp import flash, pio, registry

    _typed(tools.firmware.flash_start, "job_id", "status", "log_path")
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    done = SimpleNamespace(returncode=0, stdout="", stderr="", duration_s=0.1)
    monkeypatch.setattr(pio, "run", lambda *_a, **_k: done)
    port = "/dev/ttySCHEMA0"
    try:
        args = {"env": "tbeam", "port": port, "confirm": True}
        out = _ok(_run_tool(tools.firmware.flash_start, args))
        assert out["status"] == "running"
        deadline = time.monotonic() + 10.0
        while flash.flash_poll(out["job_id"])["status"] == "running":
            assert time.monotonic() < deadline, "the fake flash job never finished"
            time.sleep(0.02)
    finally:
        registry.clear_port_lock(port)


def test_serial_close_results_satisfy_their_schema(tools, monkeypatch) -> None:
    from meshtastic_mcp import registry, serial_session

    _typed(tools.hardware.serial_close, "ok")
    unknown = _ok(_run_tool(tools.hardware.serial_close, {"session_id": "nope"}))
    assert unknown["ok"] is False and "nope" in unknown["reason"]

    closed: list[str] = []
    monkeypatch.setattr(serial_session, "close_session", lambda s: closed.append(s.id) or True)
    registry.register_session(SimpleNamespace(id="schema-s1", port="/dev/fake"))
    try:
        out = _ok(_run_tool(tools.hardware.serial_close, {"session_id": "schema-s1"}))
    finally:
        registry.remove_session("schema-s1")
    assert out == {"ok": True} and closed == ["schema-s1"]


class _Meter:
    """`power_meter.PowerMeter` with no serial port behind it."""

    def __init__(self, port=None, *_a, **_k) -> None:
        self.port = port or "/dev/ttyMETER"

    def __enter__(self) -> _Meter:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def info(self) -> power_meter.MeterInfo:
        return power_meter.MeterInfo(self.port, "RFPowerMeterv2 1.0.11", 868)

    def version(self) -> str:
        return "RFPowerMeterv2 1.0.11"

    def read_avg_dbm(self) -> float:
        return -42.123

    def read_peak_dbm(self) -> float:
        return -40.0

    def set_freq_mhz(self, mhz: float, *, persist: bool = False) -> int:
        return 868

    def sample(self, count: int, *, interval_s: float = 0.05, peak: bool = False) -> list[float]:
        return [-60.0 - i for i in range(count)]


class _SilentMeter(_Meter):
    def info(self) -> power_meter.MeterInfo:
        raise power_meter.PowerMeterError("no reply; is it powered off?")


@pytest.mark.parametrize(
    ("meter", "found", "present"),
    [(None, None, False), (_SilentMeter, "/dev/ttyMETER", True), (_Meter, "/dev/ttyMETER", True)],
)
def test_pa_meter_status_results_satisfy_their_schema(
    tools, monkeypatch, meter, found, present
) -> None:
    _typed(tools.rf.pa_meter_status, "present")
    monkeypatch.setattr(power_meter, "find_meter_port", lambda: found)
    if meter is not None:
        monkeypatch.setattr(power_meter, "PowerMeter", meter)
    out = _ok(_run_tool(tools.rf.pa_meter_status))
    assert out["present"] is present


@pytest.mark.parametrize("peak", [False, True])
def test_pa_measure_result_satisfies_its_schema(tools, monkeypatch, peak) -> None:
    _typed(tools.rf.pa_measure, "band", "meter_cal_mhz", "kind", "min_dbm", "max_dbm")
    monkeypatch.setattr(power_meter, "PowerMeter", _Meter)
    args = {"band": "EU_868", "samples": 3, "interval_s": 0, "peak": peak, "attenuator_db": 20}
    out = _ok(_run_tool(tools.rf.pa_measure, args))
    assert out["kind"] == ("peak" if peak else "average")


@pytest.mark.parametrize(
    ("bursts", "linger", "restore_fails"),
    [
        # The floor is the median of `_Meter.sample`, about -70 dBm; -80 reads as silent.
        ([[-10.0] * 4, [-5.0] * 4], 1.0, False),  # both steps measured: the full curve
        ([[-10.0] * 4, [-80.0] * 4], None, False),  # one silent step: the short curve
        ([[-80.0] * 4, [-80.0] * 4], 1.0, True),  # nothing measured, and a failed restore
    ],
)
def test_pa_sweep_results_satisfy_their_schema(
    tools, monkeypatch, bursts, linger, restore_fails
) -> None:
    from meshtastic_mcp import pa_sweep

    _typed(tools.rf.pa_sweep, "band", "region", "table", "curve", "restore_errors")

    def set_config(path, value, port=None):
        if restore_fails and path == "lora.tx_power" and value == 13:
            raise RuntimeError("write failed")

    batches = iter(bursts)

    class _Sampler:
        def __init__(self, *_a, **_k) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> list[float]:
            return list(next(batches))

    monkeypatch.setattr(pa_sweep.admin, "set_config", set_config)
    monkeypatch.setattr(pa_sweep.admin, "send_text", lambda **_: {"ok": True, "packet_id": 1})
    monkeypatch.setattr(
        pa_sweep,
        "read_lora_context",
        lambda port=None: {"region": "EU_868", "tx_power": 13, "override_duty_cycle": False},
    )
    monkeypatch.setattr(power_meter, "PowerMeter", _Meter)
    monkeypatch.setattr(pa_sweep, "_BackgroundSampler", _Sampler)
    monkeypatch.setattr(pa_sweep.time, "sleep", lambda *_a: None)
    monkeypatch.delenv(confirm.POLICY_ENV, raising=False)

    args = {"powers": [17, 20], "confirm": True, "settle_s": 0, "burst_repeat": 1}
    out = _ok(_run_tool(tools.rf.pa_sweep, {**args, "tx_linger_s": linger}))
    assert len(out["table"]) == 2
    assert out["curve"]["points"] == sum(burst[0] > -70 for burst in bursts)
    assert bool(out["restore_errors"]) is restore_fails


@pytest.mark.parametrize("ready", [False, True])
def test_ble_sniff_status_results_satisfy_their_schema(tools, monkeypatch, tmp_path, ready) -> None:
    from meshtastic_mcp import ble_sniffer

    _typed(tools.rf.ble_sniff_status, "ready", "sniffers", "hint", "active_captures")
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    dongle = {"port": "/dev/ttyACM9", "vid": 0x1915, "pid": 0x522A, "serial_number": None}
    running = {"job_id": "abc123", "sniffer_port": "/dev/ttyACM9", "pcap_path": None}
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [dongle] if ready else [])
    monkeypatch.setattr(
        ble_sniffer, "sniffer_bin_or_none", lambda: Path("/opt/sniffer") if ready else None
    )
    monkeypatch.setattr(ble_sniffer.jobs, "running_of_kind", lambda kind: [running] * ready)
    out = _ok(_run_tool(tools.rf.ble_sniff_status))
    assert out["ready"] is ready
    assert (out["hint"] is None) is ready
