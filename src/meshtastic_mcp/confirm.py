# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Ask the human before a destructive tool runs, when the client can ask.

`confirm=True` is something an agent can pass for itself, so it is not a human gate. When a tool
is called without it and the client declared elicitation, the human is asked instead. With
`MESHTASTIC_MCP_CONFIRM=human`, irreversible tools ask even when `confirm=True` is passed. A client
that cannot ask (a subagent, `claude -p`) keeps the `confirm=True` contract and gets the same
refusal as before.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

POLICY_ENV = "MESHTASTIC_MCP_CONFIRM"
AGENT = "agent"
HUMAN = "human"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Confirm"}},
    "required": ["confirm"],
}


def policy(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    value = env.get(POLICY_ENV, AGENT).strip().lower() or AGENT
    if value not in {AGENT, HUMAN}:
        raise ValueError(f"{POLICY_ENV} must be '{AGENT}' or '{HUMAN}', got {value!r}")
    return value


def _can_ask(ctx: Context) -> bool:
    try:
        caps = ctx.session.client_capabilities
    except RuntimeError:
        return False
    return bool(caps and caps.elicitation)


def _is_modern(ctx: Context) -> bool:
    rc = ctx.request_context
    return rc is not None and rc.protocol_version in MODERN_PROTOCOL_VERSIONS


async def gate(
    ctx: Context,
    action: str,
    confirm: bool,
    *,
    irreversible: bool = False,
) -> InputRequiredResult | None:
    """Return `None` to proceed, an `InputRequiredResult` to hand back to the client, or raise.

    Call this before taking any lock or starting any job: on the modern protocol the tool is
    called again once the human answers, so the body runs from the top twice.
    """
    needs_human = not confirm or (irreversible and policy() == HUMAN)
    if not needs_human:
        return None
    if not _can_ask(ctx):
        if confirm:
            raise ToolError(f"{action} needs a human to confirm it, and this client cannot ask.")
        raise ToolError(f"{action} is destructive and requires confirm=True.")

    message = f"Allow: {action}?"
    if _is_modern(ctx):
        answers = ctx.input_responses
        if answers is None:
            return InputRequiredResult(
                result_type="input_required",
                input_requests={
                    "confirm": ElicitRequest(
                        method="elicitation/create",
                        params=ElicitRequestFormParams(message=message, requested_schema=_SCHEMA),
                    )
                },
            )
        reply = answers.get("confirm")
        content = getattr(reply, "content", None) or {}
        accepted = getattr(reply, "action", None) == "accept" and content.get("confirm") is True
    else:
        result = await ctx.elicit(message, bool)
        accepted = isinstance(result, AcceptedElicitation) and result.data is True

    if not accepted:
        raise ToolError(f"{action} was not confirmed.")
    return None
