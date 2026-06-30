# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Portable unit-tier fixtures.

Isolate the recorder data dir before any test module imports `server`/`recorder`
(server autostarts the recorder on import). Without this, the recorder's
`_default_dir()` would resolve to the platformdirs user-data dir; pinning it to a
throwaway temp dir keeps unit runs from touching real user state. `setdefault` so
an explicit override still wins.
"""

import os
import tempfile

os.environ.setdefault("MESHTASTIC_MCP_DATA_DIR", tempfile.mkdtemp(prefix="mtmcp-unit-"))
