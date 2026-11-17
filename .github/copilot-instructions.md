# GitHub Copilot instructions

See [AGENTS.md](../AGENTS.md): it is the source of truth for architecture, rules, and
workflow for all AI coding agents in this repo.

Key points for Copilot:

- **Core tools register unconditionally; firmware/android/apple tools are capability-gated.**
  Check `capabilities.detect()` before assuming a tool or module is available.
- **No firmware-tree assumptions in core.** Modules under `src/meshtastic_mcp/` must import
  and run without `MESHTASTIC_FIRMWARE_ROOT`. Use `config.firmware_root_or_none()` for guards.
- **Destructive tools are `confirm`-gated** (`reboot`, `factory_reset`, the flash and
  `nrf52_*` tools, `uhubctl_*`; full list in `docs/confirmations.md`). Each calls
  `confirm.gate()` before any lock or job, which asks the human when the client can. Don't
  remove or bypass the gate. New destructive tools must add it.
- **Failures raise `ToolError`** so the client sees `isError`; don't return `{"error": ...}`
  as a normal result.
- **Long work is a job** (`jobs.py`, a `job_id` plus a `_poll` tool), never a longer timeout.
- **One MCP call per serial port at a time** (exclusive lock): open → act → close.
- **Frame injection** (`inject_frame` / `inject.py`): delivers a crafted frame into a board's
  real receive pipeline as if it arrived off LoRa (reaches `from != 0` / decrypt / remote-admin
  paths the `toRadio` API can't). Needs firmware built with `-D
  MESHTASTIC_ENABLE_FRAME_INJECTION=1`; sim nodes always. See AGENTS.md § "Frame injection".
- **No type debt.** mypy runs with no per-module `ignore_errors`; fix types, don't exclude.
- **Gates before every push:**
  ```
  uv run ruff check . && uv run ruff format --check .
  uv run --extra dev mypy
  python3 scripts/check_spdx.py
  uv run --extra test python -m pytest tests/unit -q
  ```
- **License:** GPL-3.0-only; DCO sign-off required (`git commit -s`).
