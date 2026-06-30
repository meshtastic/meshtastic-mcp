# Contributing

Thanks for helping improve meshtastic-mcp. See [AGENTS.md](AGENTS.md) for the architecture and
capability model.

## Setup

```bash
uv sync --extra test --extra dev      # or: python -m venv .venv && .venv/bin/pip install -e '.[test,dev]'
```

## Gates (run before every PR)

```bash
ruff check .            # lint
ruff format --check .   # formatting
mypy                    # types (no per-module ignore_errors — fix types, don't exclude)
pytest tests/unit       # portable tier (hardware- and firmware-free)
```

The firmware tier needs `MESHTASTIC_FIRMWARE_ROOT`; hardware tiers need real radios. See
`run-tests.sh` and the test tiering in `tests/`.

## Branch & PR workflow

`master` is the shared integration branch. Treat its history as published:

- **Never force-push `master`** and don't rewrite published history; tags are immutable.
- Branch off the latest `master`, push your branch, and open a PR. Keep PRs focused and small.
- The gates above must pass before merge, and new behavior ships with tests.
- Resolve conflicts by rebasing your *branch* on `master` (never the other way around).
- Merge through the PR (squash or merge commit, maintainer's call) — keep `master` readable.

## Conventions

- **Capability discipline:** core modules must import and run with no firmware checkout. Gate
  firmware/emulator-only tools with the `firmware_tool` decorator / capability checks.
- **Tool annotations:** new MCP tools get `readOnlyHint`/`destructiveHint`/`openWorldHint` via
  the maps in `server.py`; destructive tools also take a `confirm` arg.
- **Commits:** Conventional Commits, signed off with DCO (`git commit -s`).
- **License:** GPL-3.0-only.
