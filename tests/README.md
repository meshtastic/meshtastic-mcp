# Meshtastic MCP Server — Test Harness

Automated test suite for the MCP server, organized around real operator
concerns rather than generic "unit vs hardware".

## Tiers

| Dir             | Hardware                | Question this tier answers                                            |
| --------------- | ----------------------- | --------------------------------------------------------------------- |
| `unit/`         | none                    | Do the parsing / filtering / profile-generation primitives work?      |
| `provisioning/` | 1 device, per-test bake | Did my pre-bake recipe stick? Does it survive a factory reset?        |
| `admin/`        | 1 device, shared bake   | Do my daily admin ops (owner, channel URL, config writes) round-trip? |
| `mesh/`         | 2 devices, shared bake  | Do my devices actually form a mesh? Send + receive? ACKs?             |
| `telemetry/`    | 2 devices, shared bake  | Is telemetry reporting? Is position broadcast correct?                |
| `monitor/`      | 1 device, shared bake   | Is the boot log clean (no panics)?                                    |
| `fleet/`        | varies                  | Are my CI runs isolated from each other? Are reflashes idempotent?    |

## Quick start

```bash
# from the repo root
pip install -e ".[test]"

# No hardware — unit tier only
pytest tests/unit -v

# Hub attached (reference bench: T-Echo, Heltec T114, RAK4631, ESP32-S3) —
# first run bakes, then exercises everything
pytest tests/ --html=report.html

# Hub already baked with session profile (dev loop) — skip bake
pytest tests/ --assume-baked --html=report.html

# Force a rebake (new firmware, new seed, etc.)
pytest tests/ --force-bake --html=report.html
```

## CLI flags

- `--force-bake` — always reflash both roles at session start, even if the
  current state matches the session profile.
- `--assume-baked` — skip `test_00_bake.py` entirely. Use when you know the
  devices are already baked and want a fast dev loop.
- `--hub-profile=<yaml>` — point at a YAML file for non-default hub hardware
  (see [docs/bench-setup.md](../docs/bench-setup.md) for a walkthrough, or
  [`example.yaml`](../example.yaml) at the repo root for every option,
  documented). Default targets VID `0x239a` (nRF52) and `0x303a`/`0x10c4`
  (ESP32-S3). As of the `session:` key below, a hub-profile YAML is a
  **complete** bench configuration on its own — no env var is required for
  anything in this file; every one below is an optional ad-hoc override on
  top of the YAML, not a requirement.
- `--no-teardown-rebake` — skip the session-end rebake that `provisioning/`
  and `fleet/` tests perform. Useful in rapid iteration.

## Environment variables

Everything below except `MESHTASTIC_FIRMWARE_ROOT` and `MESHTASTIC_MCP_SEED` has
a YAML equivalent in `--hub-profile` (see [`example.yaml`](../example.yaml)) — set it in the file
instead of exporting it, or use both: **the env var wins whenever a key is
set both places**, so a checked-in profile stays a safe shared default while
one run can still override a single value ad hoc.

- `MESHTASTIC_FIRMWARE_ROOT` — firmware repo path. Required for the bake and
  firmware-marked tiers; without it the harness auto-skips them (point it at a
  sibling `meshtastic/firmware` checkout). Not part of the hub profile — it's
  a filesystem path to a different repo, not bench/session config.
- `MESHTASTIC_MCP_ENV_<ROLE>` — PlatformIO env override per *per-board* bench
  role (`tests/_bench.py`): `MESHTASTIC_MCP_ENV_T_ECHO`,
  `MESHTASTIC_MCP_ENV_HELTEC_T114`, `MESHTASTIC_MCP_ENV_ESP32S3`,
  `MESHTASTIC_MCP_ENV_RAK4631`. Defaults come from `BENCH_ROLES`
  (`t-echo-plus`, `heltec-mesh-node-t114`, `heltec-v3`, `rak4631`). Keying per
  board — not by the collapsible VID — is what lets the three same-VID 0x239a
  nRF52 boards each get their own firmware. FleetSuite bakes these automatically
  from each connected board's pinned hub slot. **YAML:** `env:` under that
  role in `--hub-profile`.
- `MESHTASTIC_MCP_SEED` — override the session PSK seed (default:
  `pytest-<unix-ts>`). Set this to reproduce a specific failing run. No YAML
  equivalent — it's meant to change per invocation, not live in a checked-in
  profile.
- `MESHTASTIC_MCP_REGION` / `MESHTASTIC_MCP_CHANNEL_NAME` /
  `MESHTASTIC_MCP_CHANNEL_NUM` / `MESHTASTIC_MCP_MODEM_PRESET` — override the
  session `test_profile`'s region (default `US`), primary channel name
  (default `McpTest`), channel slot (default `88`), and modem preset (default
  `LONG_FAST`). `baked_mesh` only *verifies* the live device against this
  profile — it never reflashes on its own — so pointing these at a device's
  actual already-baked values (read them via `meshtastic-mcp device_info` /
  `get_config --section=lora`) lets `baked_mesh` pass **without reflashing**,
  and without ever setting a region the operator didn't choose. Use this when
  you already have firmware + config on a device (e.g. a real regulatory
  region outside the US) and don't want the harness to touch it. **YAML:**
  the top-level `session:` block in `--hub-profile` (`region:`,
  `channel_name:`, `channel_num:`, `modem_preset:` — see
  [docs/bench-setup.md](../docs/bench-setup.md#already-configured-device-skip-the-reflash-entirely)).

## Fixtures you'll use when adding tests

All defined in `conftest.py`:

- **`hub_devices`** → `{"t_echo": "/dev/cu.W", "heltec_t114": "/dev/cu.X",
  "rak4631": "/dev/cu.Y", "esp32s3": "/dev/cu.Z"}` — per-board bench roles from
  `tests/_bench.py` (hub-slot keyed; coarse VID roles are the fallback for
  custom `--hub-profile` yamls). Auto-skips the test if a required role isn't
  present.
- **`test_profile`** → USERPREFS dict for the session (`build_testing_profile`).
- **`no_region_profile`** → variant without `USERPREFS_CONFIG_LORA_REGION`.
- **`baked_mesh`** → verifies both devices are baked with the session profile
  (does NOT reflash — that's `test_00_bake.py`'s job).
- **`baked_single`** → single verified baked device; parametrize `request.param`
  to pick role.
- **`serial_capture`** → factory; `cap = serial_capture("esp32s3")` starts a
  pio device monitor session, drains into a per-test buffer, attaches the
  buffer to the pytest-html report on failure.
- **`wait_until`** → exponential-backoff polling helper; `wait_until(lambda:
predicate(), timeout=60)` replaces flaky `time.sleep()` patterns.

## Reports

`pytest --html=report.html` produces a self-contained HTML with:

- Per-test pass/fail/skip with timings
- On failure: serial log capture from any `serial_capture` fixture used
- On failure: `device_info` + lora config JSON for every role on the hub
- Session seed and session start time (for reproducibility)

`pytest --junitxml=junit.xml` produces CI-integration XML.

`tool_coverage.json` is emitted at session end in the tests directory — shows
which of the server's public MCP tools the run exercised. Useful for closing test gaps.

## Adding a new test

1. Pick the category that matches the operator concern (not the technical
   surface). "Does my fleet's owner name persist" is `admin/`, not `unit/`.
2. If you need both devices, depend on `baked_mesh`. If you need one, depend
   on `baked_single`. If you need to mutate hardware state, put it in
   `provisioning/` or `fleet/` and add a `try/finally` teardown that re-bakes
   the session profile.
3. Use `wait_until` for anything involving LoRa timing — fixed `sleep()`
   produces flakes.
4. Use `serial_capture` when you need to observe firmware log output (e.g.
   "did the packet get decoded?").
5. Add a `@pytest.mark.timeout(N)` — mesh tests routinely hit LoRa-airtime
   waits; default pytest timeout is infinite.

## Troubleshooting

- **All hardware tests SKIP** → hub not detected. Plug in the USB hub, verify
  with `pytest tests/ --collect-only` or `python -c "from meshtastic_mcp import
devices; print(devices.list_devices())"`.
- **`baked_mesh` fails with "devices not baked"** → run `pytest
tests/test_00_bake.py` first, or pass `--force-bake` on the full run.
- **Mesh formation tests time out** → check that both devices are on the same
  session profile (`--force-bake` forces both to the current seed).
- **Provisioning tests leave device in bad state** → teardowns re-bake, but
  if a test crashes between "bake broken state" and "bake good state", run
  `pytest tests/test_00_bake.py --force-bake` to recover.
