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
    library. Set ``MESHTASTIC_MCP_SDK_CLI`` (or ``MESHTASTIC_SDK_ROOT``).
    """
    return sdk_cli_mod.status()


@sdk_tool()
def sdk_device_info(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """One-shot device snapshot via the Kotlin SDK CLI (own node + node count).

    ``transport`` accepts ``tcp:host[:port]`` / ``serial:port[:baud]`` /
    ``ble:needle`` (or ``tcp://host`` / a serial device path). Returns the parsed
    ``info`` envelope plus the full envelope stream; ``ok=False`` with ``error``
    on a device-level failure. The Kotlin engine owns the link in a JVM subprocess.
    """
    return sdk_cli_mod.device_info(transport, timeout_ms=timeout_ms)


@sdk_tool()
def sdk_list_nodes(transport: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """Snapshot the device node DB via the Kotlin SDK CLI (read-only).

    Returns each node's wire-JSON under ``nodes`` (from the CLI's ``node``
    envelopes). ``transport`` syntax as in ``sdk_device_info``.
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

    Device-mutating: injects a mesh packet. ``to`` is ``BROADCAST``, a decimal
    node num, or ``0xHEX``. Waits up to ``await_ms`` for Acked/Delivered/Failed.
    """
    return sdk_cli_mod.send_text(
        transport, message, to=to, channel=channel, await_ms=await_ms, timeout_ms=timeout_ms
    )
