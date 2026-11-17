# Contributing

Thanks for helping improve meshtastic-mcp. See [AGENTS.md](AGENTS.md) for the architecture and
capability model.

## Setup

```bash
uv sync --extra test --extra dev      # or: python -m venv .venv && .venv/bin/pip install -e '.[test,dev]'
```

## Gates (run before every PR)

```bash
ruff check .                  # lint
ruff format --check .         # formatting
mypy                          # types (no per-module ignore_errors — fix types, don't exclude)
python scripts/check_spdx.py  # SPDX headers on every source file
pytest tests/unit             # portable tier (hardware- and firmware-free)
```

Touching the FleetSuite SPA? Also run `cd web-ui && npx vue-tsc --noEmit && npm run build`.

The firmware tier needs `MESHTASTIC_FIRMWARE_ROOT`; hardware tiers need real radios. See
`run-tests.sh` and the test tiering in `tests/`.

## Conventions

- **Capability discipline:** core modules must import and run with no firmware checkout. Gate
  firmware/emulator-only tools with the `firmware_tool` decorator / capability checks.
- **Tool annotations:** a new MCP tool goes in its group's module under `tools/` and gets
  `readOnlyHint`/`destructiveHint`/`openWorldHint` via the maps in `server.py` and a group in
  `toolsets.py`; destructive tools also take a `confirm` arg and call `confirm.gate` before any
  side effect (see `docs/confirmations.md`).
- **Commits:** Conventional Commits, signed off with DCO (`git commit -s`).
- **License:** GPL-3.0-only. Every source file carries the SPDX header (CI enforces it):
  `SPDX-FileCopyrightText: Meshtastic contributors` + `SPDX-License-Identifier: GPL-3.0-only`.
- **Dependencies:** a dependency that exists only on git goes in a `[dependency-groups]` group,
  never an extra. PyPI rejects a package whose metadata names a URL; `uv build` in CI refuses one.

## Releasing

The version comes from the git tag (hatch-vcs). `server.json`'s checked-in version is a
placeholder that `release.yml` overwrites from the tag.

1. In a PR, rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD` above a fresh
   empty `## [Unreleased]`, and merge it.
2. Tag that PR's merge commit and push the tag:
   `git tag -a vX.Y.Z -m vX.Y.Z <merge-sha> && git push origin vX.Y.Z`.
   `release.yml` builds, publishes to PyPI by trusted publishing, then creates the GitHub release
   and publishes `server.json` to the MCP Registry.
3. Check all three: the version on `https://pypi.org/project/meshtastic-mcp/`, the GitHub release,
   and `https://registry.modelcontextprotocol.io/v0/servers?search=io.github.meshtastic/meshtastic-mcp`.

A failed `publish-pypi` job means nothing shipped: the GitHub release and the Registry wait on it.
Fix on `master`, delete the remote tag (`git push origin :refs/tags/vX.Y.Z`) and tag the fix's
merge commit with the same version. Once PyPI has accepted a version it never takes that version
again, so after a successful publish, fix forward with a new version.
