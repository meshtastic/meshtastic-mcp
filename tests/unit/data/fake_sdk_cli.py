# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Stands in for `cli mcp`: the SDK CLI's four tools over stdio, with a counter of connects."""

import json
import os
import sys

from fastmcp import FastMCP

app = FastMCP("fake-sdk-cli")
state = {"connects": 0, "target": None}
LOG = os.environ.get("FAKE_SDK_LOG")


@app.tool()
def connect(transport: str, timeout_ms: int = 30000) -> str:
    state["connects"] += 1
    state["target"] = transport
    if LOG:
        with open(LOG, "a") as f:
            f.write(f"connect {transport}\n")
    return json.dumps({"state": "Connected", "transport": transport, "node_count": 2})


@app.tool()
def connection_state() -> str:
    if state["target"] is None:
        return json.dumps({"state": "NoSession"})
    return json.dumps({"state": "Connected", "own": "!3a14aa13", "node_count": 2})


@app.tool()
def nodes(limit: int | None = None) -> str:
    rows = [
        {"id": "!3a14aa13", "long_name": "Heltec", "short_name": "HV3", "hw_model": "HELTEC_V3"},
        {"id": "!c2c91376", "long_name": "RAK", "hw_model": "RAK4631", "hops_away": 1, "snr": 6.5},
    ]
    return json.dumps({"count": 2, "nodes": rows})


if __name__ == "__main__" and sys.argv[1:] == ["mcp"]:
    app.run()
