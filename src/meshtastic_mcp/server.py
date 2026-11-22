# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The MCP server: the `FastMCP` app with every tool in `tools/` registered on it.

Importing it registers the tools, adds the toolset, lease and annotation transforms, starts
the recorder and registers the resources and prompts. Each tool's hints and lease handling
come from its `@meta(...)` declaration (`tool_meta`). Import a tool or helper from the module
that defines it; tests patch it there too.
"""

from __future__ import annotations

import itertools
import logging
import os
from collections.abc import Sequence
from typing import Any

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

from . import backends, capabilities, connection, leases, recorder_query, tool_meta, toolsets
from . import doctor as doctor_mod
from .call_log import CallLogMiddleware
from .recorder import get_recorder
from .retired_tools import RetiredToolMiddleware

# Importing each tool module registers its tools on `app`.
from .tools import (  # noqa: F401
    android,
    app_drive,
    apple,
    core,
    firmware,
    hardware,
    replay,
    rf,
    sdk,
)
from .tools._app import CAPS, GATED, app

log = logging.getLogger(__name__)


app.add_transform(toolsets.ToolsetTransform(os.environ, tool_meta.read_only_names))
app.add_middleware(CallLogMiddleware())
app.add_middleware(RetiredToolMiddleware())


def _start_recorder() -> None:
    # Persistent device-log capture. Starts on first import — pubsub fan-out
    # is process-global, so subscribing here captures every active interface
    # (whether opened by an MCP tool, a pytest fixture, or a serial_session).
    # Files land in mcp-server/.mtlog/ (gitignored). See recorder/recorder.py
    # for the full design. Recorder startup is best-effort: an unwritable
    # log dir or pubsub mismatch should not take the MCP server down.
    try:
        get_recorder().start()
    except Exception as exc:
        log.warning("Failed to start persistent recorder: %s", exc)


_start_recorder()


_INACTIVE_HINTS = {
    "firmware": "set MESHTASTIC_FIRMWARE_ROOT to enable",
    "mvgrind": "build https://github.com/miketweaver/mvgrind or set $MESHTASTIC_MCP_MVGRIND; "
    "vanity_preview and vanity_apply still work",
    "sdr": "install the 'sdr' extra, librtlsdr and an RTL-SDR to enable",
    "ble_sniffer": "attach an nRF Sniffer dongle to enable; ble_sniff_status still works",
}


def _log_capabilities() -> None:
    try:
        log.info("meshtastic-mcp capabilities active: %s", CAPS.summary())
        active = {
            "firmware": CAPS.firmware,
            "android": CAPS.android,
            "apple": CAPS.apple,
            "sdr": CAPS.sdr,
            "ble_sniffer": CAPS.ble_sniffer,
            "sdk_cli": CAPS.sdk_cli,
            "mvgrind": CAPS.mvgrind,
            "app_plane": CAPS.android or CAPS.apple,
        }
        for cap, names in sorted(GATED.items()):
            if active.get(cap, True):
                continue
            hint = _INACTIVE_HINTS.get(cap)
            log.info(
                "%s capability inactive: %d tools not registered%s: %s",
                cap,
                len(names),
                f" ({hint})" if hint else "",
                ", ".join(names),
            )
    except Exception as exc:  # never fail startup over a capability probe
        log.warning("capability detection failed: %s", exc)


_log_capabilities()


class _AnnotationTransform(Transform):
    """Stamp each tool's `@meta` hints onto it wherever the server lists or resolves it."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t.model_copy(update={"annotations": tool_meta.hints(t.name)}) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None:
            return None
        return tool.model_copy(update={"annotations": tool_meta.hints(tool.name)})


_PORT_FLAGS = ("--port", "-p")


def _port_in_args(args: Any) -> str | None:
    """The one port `args` names; a tool takes the last of several, so two are refused."""
    if not isinstance(args, list):
        return None
    found = {
        value
        for flag, value in itertools.pairwise(args)
        if flag in _PORT_FLAGS and isinstance(value, str)
    }
    found |= {
        a.split("=", 1)[1]
        for a in args
        if isinstance(a, str) and a.startswith(tuple(f + "=" for f in _PORT_FLAGS))
    }
    if len(found) > 1:
        raise ToolError(f"args name more than one port ({', '.join(sorted(found))}); pass one.")
    return next(iter(found), None)


def _takes_device_port(tool: Tool) -> bool:
    props = (tool.parameters or {}).get("properties", {})
    mode = tool_meta.of(tool.name).port
    if mode == "finds_own":
        return True
    return ("port" in props or "node" in props) and mode == "device"


async def _with_lease_for(tool_name: str, lease: str | None, kwargs: dict[str, Any]) -> Any:
    from fastmcp.tools.tool_transform import forward

    port = kwargs.get("node", kwargs.get("port")) or _port_in_args(kwargs.get("args"))
    try:
        # A BLE radio has no lease key until the registry records its BLE identity.
        resolved = None if (port or "").startswith("ble:") else connection.resolve_port(port)
    except connection.ConnectionError:
        resolved = None  # the tool reports the missing or ambiguous port itself
    try:
        if resolved is not None:
            await anyio.to_thread.run_sync(leases.check, resolved, lease)
        elif not port and tool_meta.of(tool_name).port == "finds_own":
            await anyio.to_thread.run_sync(leases.check_any, lease)
    except leases.LeaseError as exc:
        raise ToolError(str(exc)) from exc
    token = leases.CURRENT.set(lease)
    try:
        return await forward(**kwargs)
    finally:
        leases.CURRENT.reset(token)


_LEASE_ARG_DOC = (
    "The handle from `claim` while you hold this device; omit for a device nobody has claimed."
)


def _lease_aware(tool: Tool) -> Tool:
    if not _takes_device_port(tool):
        return tool
    name = tool.name

    async def _with_lease(lease: str | None = None, **kwargs: Any) -> Any:
        return await _with_lease_for(name, lease, kwargs)

    wrapped = Tool.from_tool(tool, transform_fn=_with_lease)
    props = dict(wrapped.parameters.get("properties", {}))
    if "lease" in props:
        props["lease"] = {**props["lease"], "description": _LEASE_ARG_DOC}
        wrapped = wrapped.model_copy(
            update={"parameters": {**wrapped.parameters, "properties": props}}
        )
    return wrapped


class _LeaseTransform(Transform):
    """Give every device tool a `lease` argument and refuse a call on someone else's lease."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_lease_aware(t) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return None if tool is None else _lease_aware(tool)


app.add_transform(_LeaseTransform())
app.add_transform(_AnnotationTransform())


# ---------- MCP resources (readable context, no tool round-trip) -----------
@app.resource(
    "meshtastic://doctor",
    name="Environment doctor report",
    mime_type="application/json",
)
def _resource_doctor() -> str:
    """Live capability + dependency report (same data as the `doctor` tool)."""
    import json

    return json.dumps(doctor_mod.run().to_dict(), indent=2)


@app.resource(
    "meshtastic://capabilities",
    name="Active capabilities",
    mime_type="text/plain",
)
def _resource_capabilities() -> str:
    """One-line summary of which capability groups are active on this host."""
    return capabilities.detect().summary()


@app.resource(
    "meshtastic://recorder/schema",
    name="Recorder query schema",
    mime_type="application/json",
)
def _resource_recorder_schema() -> str:
    """The views the `query` tool reads, with each column's DuckDB type."""
    import json

    return json.dumps(recorder_query.schema(), indent=2)


@app.resource(
    "meshtastic://device/info",
    name="Live device info",
    mime_type="application/json",
)
def _resource_device_info() -> str:
    """Live device summary (firmware/region/node id) for the auto-selected port.

    Reads the single connected device. For multi-device hosts use the
    `inspect` tool with an explicit `node` instead.
    """
    import json

    try:
        read = backends.for_target(None).read(None, ["summary"], None, "brief", 8.0)
        return json.dumps(read.get("summary") or read, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://device/nodes",
    name="Live mesh node database",
    mime_type="application/json",
)
def _resource_device_nodes() -> str:
    """Live node DB (local node + peers) for the auto-selected port."""
    import json

    try:
        read = backends.for_target(None).read(None, ["nodes"], None, "brief", 8.0)
        return json.dumps(read.get("nodes") or read, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://e2e/{loop}",
    name="e2e loop recipe",
    mime_type="text/markdown",
)
def _resource_e2e_loop(loop: str) -> str:
    """A bundled meshtastic-e2e reference doc (e.g. loop-inbound, harness, topology)."""
    import pathlib

    base = pathlib.Path(__file__).parent / "skills" / "meshtastic-e2e" / "references"
    name = loop if loop.endswith(".md") else f"{loop}.md"
    path = (base / name).resolve()
    if not path.is_file() or base.resolve() not in path.parents:
        avail = sorted(p.stem for p in base.glob("*.md"))
        return f"unknown loop {loop!r}. available: {', '.join(avail)}"
    return path.read_text(encoding="utf-8")


# ---------- MCP prompts (pre-baked agent workflows) ------------------------
@app.prompt(title="Triage an e2e failure")
def triage_e2e_failure(token: str = "", deadline_iso: str = "") -> str:
    """Guide a dual-plane root-cause analysis of a failed e2e loop."""
    return (
        "An e2e loop FAILED. Find the root cause by correlating the two planes:\n"
        f"- token: {token or '<the unique marker token>'}\n"
        f"- deadline: {deadline_iso or '<wall-clock of the assertion>'}\n\n"
        "1. Device plane (source of truth): `query` the recorder's `packets` and `logs` views\n"
        "   around the deadline (filter on `ts`). Did a TEXT_MESSAGE_APP carrying the token\n"
        "   actually reach the wire? Check for NAK/err=5 (MAX_RETRANSMIT) on broadcasts.\n"
        "2. App plane: inspect the app `layout`/`screen capture` at the deadline. Was the app\n"
        "   connected (Subscribed)? On the right screen? Was the token rendered but missed?\n"
        "3. Align by shared epoch timestamps. Classify: never-sent / sent-not-received /\n"
        "   received-not-rendered / rendered-not-asserted.\n"
        "4. Emit a one-line root-cause hypothesis + the minimal repro. See meshtastic://e2e/harness."
    )


@app.prompt(title="Bring up a device")
def bringup_device(port: str = "", region: str = "US") -> str:
    """Connect to a Meshtastic device and verify a healthy baseline."""
    return (
        f"Bring up the Meshtastic device{' at ' + port if port else ''}:\n"
        "1. `devices` to find it (or use the given port / a tcp://host:port).\n"
        "2. `inspect` for firmware version + node identity; `inspect(what=['nodes'])` for\n"
        "   the node DB.\n"
        f"3. Confirm region is set ({region}); if unset, `configure` `lora.region`\n"
        "   (dry_run first).\n"
        "4. `send` a text marker to ^all and confirm it leaves on the wire.\n"
        "Report firmware version, region, node count, and any config that looks off."
    )


@app.prompt(title="Run the inbound e2e loop")
def inbound_loop() -> str:
    """Drive the device->app inbound loop and report a verdict."""
    return (
        "Run the inbound (device->app) e2e loop. Read meshtastic://e2e/loop-inbound and\n"
        "meshtastic://e2e/harness first. Bring up the virtual mesh, connect the app over TCP,\n"
        "broadcast a unique marker token from the tester, and assert it renders in the app.\n"
        "Prefer a journey (meshtastic://e2e/journeys) over hardcoded taps. Respect the hard\n"
        "rules: unique token, bounded polling, the recorder is the device-side oracle. Emit\n"
        "`LOOP inbound PASS|FAIL token=... latency=...`."
    )


@app.prompt(title="Compare two firmware versions")
def compare_firmware(ref_a: str = "", ref_b: str = "") -> str:
    """Assess the app-facing behavioral delta between two firmware refs (impact agent)."""
    return (
        f"Compare firmware {ref_a or '<ref A / baseline>'} vs {ref_b or '<ref B / candidate>'} "
        "for app-facing impact:\n"
        "1. Build each: `scripts/build_meshtasticd.sh --env native-macos --ref <ref>` (records\n"
        "   the resolved sha). Or use the CI `firmware_ref` input.\n"
        "2. For each build, run the inbound + outbound + node-sync loops against the SAME app\n"
        "   build (pin it), capturing the device-plane recorder windows + the app verdicts.\n"
        "3. Diff: protobuf/config changes, new/renamed packet fields, timing/latency, any loop\n"
        "   that flips PASS<->FAIL. Read the release notes for migration notes where relevant.\n"
        "4. Report: a per-loop verdict table (A vs B) + a short app-facing-impact summary and the\n"
        "   two resolved shas. Flag anything that would break the current app."
    )
