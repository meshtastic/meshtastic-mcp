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
