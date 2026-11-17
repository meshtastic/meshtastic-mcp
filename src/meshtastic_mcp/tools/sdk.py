# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SDK tools: device IO through the Kotlin SDK's headless CLI (experimental)."""

from __future__ import annotations

from typing import Any

from .. import sdk_cli as sdk_cli_mod
from ._app import sdk_tool


@sdk_tool()
def sdk_status() -> dict[str, Any]:
    """Report the Kotlin-SDK device-IO bridge: whether the CLI launcher resolves.

    Experimental alternative device backend that shells out to the meshtastic-sdk
    headless JVM CLI (BLE/TCP/serial engine) instead of the Python `meshtastic`
    library. Set `MESHTASTIC_MCP_SDK_CLI` (or `MESHTASTIC_SDK_ROOT`).

    Returns `{available, cli_path, env}`.
    """
    return sdk_cli_mod.status()


@sdk_tool()
def sdk_device_info(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """One-shot device snapshot via the Kotlin SDK CLI (own node + node count).

    The Kotlin engine owns the link in a JVM subprocess. A device-level failure comes
    back as `ok=False` with `error`.

    Returns `{ok, exit, info, by_type, envelopes, error, stderr}`, where `info` is the
    parsed `info` envelope and `envelopes` the full stream.

    Args:
        transport: `tcp:host[:port]`, `serial:port[:baud]` or `ble:needle`; `tcp://host`
            or a serial device path works too.
        timeout_ms: CLI timeout for the device operation, in milliseconds.
    """
    return sdk_cli_mod.device_info(transport, timeout_ms=timeout_ms)


@sdk_tool()
def sdk_list_nodes(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """Snapshot the device node DB via the Kotlin SDK CLI (read-only).

    Returns `{ok, exit, nodes, by_type, envelopes, error, stderr}`, with each node's
    wire-JSON under `nodes` (from the CLI's `node` envelopes).

    Args:
        transport: Device link, syntax as in `sdk_device_info`.
        timeout_ms: CLI timeout for the device operation, in milliseconds.
    """
    return sdk_cli_mod.list_nodes(transport, timeout_ms=timeout_ms)


@sdk_tool()
def sdk_send_text(
    transport: str,
    message: str,
    to: str = "BROADCAST",
    channel: int = 0,
    await_ms: int = 30000,
    timeout_ms: int = 15000,
) -> dict[str, Any]:
    """Transmit a text message via the Kotlin SDK CLI and await the outcome.

    Device-mutating: injects a mesh packet. Waits up to `await_ms` for
    Acked/Delivered/Failed.

    Returns `{ok, exit, by_type, envelopes, error, stderr}`.

    Args:
        transport: Device link, syntax as in `sdk_device_info`.
        message: Text to send.
        to: `BROADCAST`, a decimal node num, or `0xHEX`.
        channel: Channel index to send on.
        await_ms: How long to wait for Acked/Delivered/Failed, in milliseconds.
        timeout_ms: CLI timeout for the device operation, in milliseconds.
    """
    return sdk_cli_mod.send_text(
        transport, message, to=to, channel=channel, await_ms=await_ms, timeout_ms=timeout_ms
    )
