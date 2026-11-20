# Setting up your own hardware bench

The hardware test tiers and FleetSuite drive real boards on a USB hub. This
guide takes you from "boards in a drawer" to a green bench run on **your**
hardware — you do not need the reference bench's exact boards.

## What you need

- 1–4 Meshtastic-capable dev boards (any mix; two minimum for the mesh tiers)
- A **PPPS-capable** USB hub (per-port power switching) if you want power-cycle
  recovery and the `recovery/` tier — check yours with `uhubctl`. Without one,
  everything still runs except power-cycle recovery.
- A firmware checkout for the bake/flash tiers:
  `git clone https://github.com/meshtastic/firmware` and set
  `MESHTASTIC_FIRMWARE_ROOT` to it.
- This repo cloned (the test suite lives in `tests/`, not the wheel):
  `pip install -e ".[test,web]"`.

## 1. See what's connected

```bash
meshtastic-mcp devices          # ports, VIDs, likely_meshtastic
uhubctl                         # hub topology + PPPS support (optional)
```

Or start FleetSuite (`meshtastic-mcp-web --browser`) — discovery enriches every
board with its exact `hw_model`, firmware, and PlatformIO env, and shows the
hub slot each board hangs off. This is the fastest way to collect the values
the profile below needs.

## 2. Write a hub profile

A hub profile maps *bench roles* → *your physical boards*. Roles are what the
tests parametrize over; a role not in your profile simply skips its tests.

```yaml
# my-bench.yaml — example: two boards on hub slots 1-2.3:1 and 1-2.3:3
rak4631:
  vid: 0x239a            # USB VID (fallback matcher)
  location: "1-2.3.1"    # sysfs USB location — pins the exact hub slot
  env: rak4631           # PlatformIO env to bake/flash for this board
esp32s3:
  vid: 0x10c4
  alt_vids: [0x303a]     # accept native-USB S3s too
  location: "1-2.3.3"
  env: heltec-v3
```

- `location` is what disambiguates same-VID boards (three nRF52 boards all
  enumerate as `0x239a`). Get it from FleetSuite's device card or
  `ls /sys/bus/usb/devices/`. With a location pin, recovery power-cycles the
  correct slot.
- `env` must match the board's real hardware — the bake flashes it. When in
  doubt check FleetSuite's enriched `env` field (resolved from the board's own
  `hw_model` over the wire, not guessed from the VID).
- Role names are free-form, but reusing the reference names
  (`rak4631`, `esp32s3`, `t_echo`, `heltec_t114`) keeps you aligned with the
  reference bench's parametrization.

### Already-configured device? Skip the reflash entirely

`baked_mesh` (the fixture behind every hardware test) only *verifies* a
device's live region/channel against the harness's test-mesh profile — it
never reflashes on its own. By default that profile is a fixed isolated
mesh (`US` / channel `McpTest` / slot 88), which won't match a device you
configured yourself. Rather than let the harness reflash your board into
that profile, tell it what's *already* there with a top-level `session:`
block in your hub profile:

```yaml
# my-bench.yaml
session:
  region: EU_868 # read via: meshtastic-mcp device_info --port=<port>
  channel_name: FAB26 # same
  channel_num: 0 # read via: meshtastic-mcp get_config --section=lora --port=<port>

esp32s3:
  vid: 0x303a
```

With this, `pytest tests/ --hub-profile=my-bench.yaml --assume-baked` runs
against the device exactly as it already is — no reflash, and no risk of
the harness silently switching a device to a regulatory region you didn't
choose. Every `session:` key is also settable via an env var
(`MESHTASTIC_MCP_REGION` etc.) for a one-off override without editing the
file — the env var wins when both are set. See `example.yaml` at the repo
root for every option, documented.

## 3. Run it

```bash
export MESHTASTIC_FIRMWARE_ROOT=~/meshtastic/firmware

# The tiered suite against your profile (first run bakes = flashes; slow):
pytest tests/ --hub-profile=my-bench.yaml --html=report.html

# Skip the bake once the bench is provisioned (fast dev loop):
pytest tests/ --hub-profile=my-bench.yaml --assume-baked

# Or drive it from the FleetSuite UI / the TUI:
meshtastic-mcp-web --browser        # Tests tab
meshtastic-mcp-test-tui
```

`run-tests.sh` (no profile needed) auto-detects boards by handshaking each one
(`hw_model` → exact env) — safe on any bench, but hub-slot pinning via a
profile is what enables per-slot power-cycle recovery.

## Safety notes

- The **bake reflashes your boards** with the session's test firmware and
  wipes their config. Don't point the harness at a radio you care about.
- Verified-over-guessed: anything that flashes resolves the env from the
  board's actual `hw_model` when it can. If the harness reports a role as
  `[unverified]`, confirm the board before trusting a flash.
- Region defaults come from the test profile (`US`) — set your own regulatory
  region in the profile overrides if you're not in the US, since the mesh
  tiers transmit on air.

## Troubleshooting

- **Role not detected** — check `meshtastic-mcp devices` shows the port; a
  same-VID board may have been claimed by an earlier role (pin `location`).
- **Port stuck / EIO** — FleetSuite's per-device *Unwedge* frees it (waits out
  holders, power-cycles the slot if the hub supports PPPS). CLI equivalent:
  the `recover_device` MCP tool, or replug.
- **`doctor`** — `meshtastic-mcp doctor` lists every missing dependency with
  the exact install command.

## FleetSuite

FleetSuite is a local web UI for a multi-board bench: a live device registry (discovery plus firmware, hardware model and region enrichment), a build queue with flashing, an escalating recovery ladder (reboot, USB power-cycle, bootloader, reflash), per-device camera streams for screen assertions, the tiered test runner, serial monitors and optional Datadog log and metric shipping.

```bash
uv tool install 'meshtastic-mcp[web]'
meshtastic-mcp-web              # desktop window at http://127.0.0.1:8765
meshtastic-mcp-web --browser    # serve only, headless or to open yourself
```

It binds to `127.0.0.1` by default, and its destructive actions (reflash, factory reset) ask for confirmation. While it runs, FleetSuite owns the bench's serial ports through its monitors and enrichment. Pause it or use its own controls rather than pointing a second tool at the same ports; its test runner suspends the monitors for the length of a run. From a source checkout, `./scripts/fleetsuite.sh` bootstraps the venv, the npm packages and the SPA build in one command, and `./scripts/web-dev.sh` runs the backend with the Vite dev server and hot reload. Set `MESHTASTIC_FIRMWARE_ROOT` to enable builds, reflash recovery and exact per-board PlatformIO env resolution; without it FleetSuite still discovers, enriches and drives devices.

## The hardware test suite

`tests/` is a tiered pytest suite (`unit`, `mesh`, `telemetry`, `monitor`, `recovery`, `ui`, `fleet`, `admin`, `provisioning`). `unit` runs with no hardware. The hardware tiers target a USB hub with per-board roles keyed by hub slot; the reference bench is a T-Echo, a Heltec T114, a RAK4631 and an ESP32-S3 (see `tests/_bench.py` and [tests/README.md](../tests/README.md)). Run it through `run-tests.sh`, the `meshtastic-mcp-test-tui` terminal UI or FleetSuite's test runner.
