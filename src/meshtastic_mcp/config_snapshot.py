# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Config snapshots + diffing.

Capture a device's full config (localConfig + moduleConfig) to a named JSON
snapshot under the MCP data dir, then diff two snapshots or a snapshot against
the live device. Useful for:

- "What did this firmware upgrade change?" (snapshot before, upgrade, diff)
- Provisioning verification (snapshot a golden device, diff field deployments)
- Regression checks after a `set_config` batch

Snapshots live in `$MESHTASTIC_MCP_DATA_DIR/snapshots/<name>.json` (platformdirs
fallback). Each is `{name, captured_at, port, config: {localConfig, moduleConfig}}`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import admin


def _snapshot_dir() -> Path:
    from platformdirs import user_data_dir

    root = Path(os.environ.get("MESHTASTIC_MCP_DATA_DIR") or user_data_dir("meshtastic-mcp"))
    d = root / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_path(name: str) -> Path:
    # Guard against path traversal in the user-supplied name.
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", "."))
    if not safe or safe != name:
        raise ValueError(f"Invalid snapshot name {name!r}: use [A-Za-z0-9._-] only.")
    return _snapshot_dir() / f"{safe}.json"


def capture(name: str, port: str | None = None) -> dict[str, Any]:
    """Capture the device's full config to a named snapshot.

    Overwrites an existing snapshot of the same name. Returns metadata about
    what was captured (counts, path).
    """
    path = _snapshot_path(name)
    cfg = admin.get_config(section=None, port=port)  # full config
    snapshot = {
        "name": name,
        "captured_at": time.time(),
        "port": port,
        "config": cfg.get("config", cfg),
    }
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    config = snapshot["config"]
    local = config.get("localConfig", {})
    module = config.get("moduleConfig", {})
    return {
        "ok": True,
        "name": name,
        "path": str(path),
        "local_sections": sorted(local.keys()),
        "module_sections": sorted(module.keys()),
    }


def list_snapshots() -> list[dict[str, Any]]:
    """List saved snapshots with their capture time and port."""
    out: list[dict[str, Any]] = []
    for p in sorted(_snapshot_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                {
                    "name": data.get("name", p.stem),
                    "captured_at": data.get("captured_at"),
                    "port": data.get("port"),
                    "path": str(p),
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _load(name: str) -> dict[str, Any]:
    path = _snapshot_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"No snapshot named {name!r}. Use capture() first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested config dict to dot-path keys for field-level diffing."""
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


def diff(name_a: str, name_b: str | None = None, port: str | None = None) -> dict[str, Any]:
    """Diff two snapshots, or a snapshot against the live device.

    If `name_b` is None, snapshot `name_a` is diffed against the current live
    device config (captured on the fly). Returns added/removed/changed fields
    by dot-path (e.g. "localConfig.lora.region").
    """
    config_a = _load(name_a)["config"]
    if name_b is None:
        live = admin.get_config(section=None, port=port)
        config_b = live.get("config", live)
        label_b = "live"
    else:
        config_b = _load(name_b)["config"]
        label_b = name_b

    flat_a = _flatten(config_a)
    flat_b = _flatten(config_b)
    keys_a, keys_b = set(flat_a), set(flat_b)

    changed = {
        k: {"from": flat_a[k], "to": flat_b[k]} for k in keys_a & keys_b if flat_a[k] != flat_b[k]
    }
    added = {k: flat_b[k] for k in keys_b - keys_a}
    removed = {k: flat_a[k] for k in keys_a - keys_b}

    return {
        "from": name_a,
        "to": label_b,
        "changed": changed,
        "added": added,
        "removed": removed,
        "identical": not (changed or added or removed),
    }
