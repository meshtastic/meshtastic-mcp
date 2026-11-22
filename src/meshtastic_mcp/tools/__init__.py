# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The MCP tools, one module per toolset group in `toolsets.py`.

Each tool handler is a thin delegation to a named module (pio.py, the backends, etc.); business
logic does not live here. `_app` holds the `FastMCP` app and the capability decorators, and
`_shared` the helpers several modules use. Importing a module registers its tools on `app`;
`server` imports every one.
"""
