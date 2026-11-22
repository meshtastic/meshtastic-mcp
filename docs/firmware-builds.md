# Firmware builds and flashes

`install` flashes any board from a build or a file and is on by default. The build, board and userPrefs tools register when `MESHTASTIC_FIRMWARE_ROOT` points at a firmware checkout and PlatformIO's `pio` is installed; they form the opt-in `firmware` toolset (`MESHTASTIC_MCP_TOOLSETS=all`). How jobs, polling and MCP background tasks work is in [safety.md](safety.md#jobs-and-long-running-work); how the confirmation gate works is in [safety.md](safety.md#the-confirmation-gate).

## One tree for every session

`MESHTASTIC_FIRMWARE_ROOT` is one checkout, set the same way for every session and worktree on the machine, so concurrent agents build in one `.pio` tree. PlatformIO rewrites `project.checksum` and cleans `.pio/build/*` when the env changes, so another session's build silently deletes yours, even between a build finishing and the next `ls`. Treat a finished build as perishable: flash it at once, and before a multi-device session check for other builders with `pgrep -af 'pio run'`.

## Building

`build` runs `pio run -e <env>` and returns `job_id`, `status`, `exit_code`, `duration_s`, the artifact paths under `.pio/build/<env>/`, the last 200 log lines, `worker_error` and the `build_flags` it used. A failed build is a tool error carrying the log tail. A client without MCP task support blocks for the whole compile; `job(job_id, wait_s=...)` follows a build whose call was abandoned.

| Argument | Effect |
| --- | --- |
| `with_manifest` | `True` (the default) adds the `mtjson` target, which writes an `.mt.json` manifest beside the firmware. |
| `userprefs` | A dict of `USERPREFS_<KEY>: value` baked into this build by injecting it into `userPrefs.jsonc`; the file is restored after the build. `userprefs_manifest` lists the keys; `userprefs_set` makes a lasting change. |
| `build_flags` | A dict of `-D<NAME>=<VALUE>` macros for this build only, passed through `PLATFORMIO_BUILD_FLAGS`. |

`build_flags={"DEBUG_HEAP": 1}` turns on per-thread leak detection and puts a `[heap N]` prefix on every log line. The recorder picks the prefix up and builds a high-resolution heap timeline, which `query` reads from the `telemetry` table beside the normal LocalStats packets (about one a minute). Pair it with `/leakhunt` to classify a leak.

`clean` runs `pio run -e <env> -t clean`, for a branch switch or a stale-cache build failure.

## Flashing

`install(port, source, ...)` picks the flasher from the board and the source, waits for the board to answer, and returns the `firmware_version` it now reports.

| `source` | What it writes |
| --- | --- |
| `{"env": "<pio env>"}` on ESP32 | Builds, then an OTA update of the application partition that keeps config and the node database. With `erase=True`, wipes the whole flash and writes the bootloader, app, OTA and LittleFS partitions from the `factory.bin`; a blank ESP32 wants this. |
| `{"env": ...}` on nRF52 or RP2040 | Builds, then `pio run -t upload`, which does the bootloader touch. |
| `{"file": "<x>.uf2"}` | An nRF52 image or bootloader update through the UF2 drive, entering UF2 mode first. |
| `{"file": "<x>.zip"}` | nRF52 serial DFU through nrfutil. |
| `{"file": "<x>.bin"}` | An ESP32 image written at 0x0 by esptool; only a `firmware-*.factory.bin` is accepted there. |

- `userprefs` and `build_flags` apply to the build `install` runs. Pass build flags to the install, not only to an earlier `build`: the rebuild before upload relinks without them otherwise.
- `install` asks first (`confirm=True` once the user agreed), and `erase=True` is irreversible.
- It runs as a `flashes` job. A flash cannot be stopped mid-write, so after a cancel or timeout follow its `job_id` with `job(job_id, wait_s=...)` before touching the board.

For chip-level work, `vendor(tool="esptool"|"nrfutil"|"picotool", args=[...])` runs the vendor tool directly. Read-only subcommands run without asking; anything else asks.

## userPrefs

`userprefs_manifest` combines `userPrefs.jsonc` (active keys and commented examples) with a scan of `src/**` for `USERPREFS_<KEY>` references, so every key the firmware consumes shows up, even one the jsonc does not document. Each entry has `key`, `active` (uncommented), `value`, `example` (the commented default), `declared_in_jsonc`, `consumed_by` (source files) and `inferred_type`. `inferred_type` mirrors how `platformio-custom.py` wraps the value at build time:

| `inferred_type` | Build-time form |
| --- | --- |
| `brace` | a byte array, `{ 0x01, ... }` |
| `number` | decimal |
| `bool` | `true` / `false` |
| `enum` | a `meshtastic_*` constant |
| `string` | wrapped in quotes by `StringifyMacro` |
| `unknown` | not inferred |

`userprefs_testing_profile` returns a userPrefs dict for an isolated test mesh (a private channel on a non-default slot, MQTT off), ready for the `userprefs` argument of `build` or `install`. Give every device in a cluster the same `psk_seed`.

## Fake node databases

`push_fake_nodedb` pushes a fake-NodeDB v25 fixture of 250, 500, 1000 or 2000 nodes:

- `target="portduino"` copies the file to `~/.portduino/<portduino_config>/prefs/nodes.proto`. It is fast and needs no device connection.
- `target="hardware"` uploads it over XModem (serial or BLE) to `/prefs/nodes.proto`. It needs `port` and `confirm=True`, or the user's answer when the client can ask, and reboots the device so `loadFromDisk` picks the file up.

Each call compiles a proto with fresh timestamps from the committed JSONL seed, `test/fixtures/nodedb/seed_v25_<N>.jsonl`, so the node DB always looks recent to a connecting phone, while the structural data (names, ids, positions, telemetry) stays deterministic. `custom_seed_jsonl` pushes a hand-edited scenario instead.

## nRF52: UF2 flashing and recovery

Boards on the Meshtastic nRF52 bootloader show a USB drive in UF2 mode. Copying a `.uf2` onto it is several times faster than serial DFU and needs no 1200 baud touch. These tools are in the `hardware` toolset.

```text
nrf52_enter_uf2(port=P)                         # reboot into the UF2 drive (asks first)
nrf52_bootloader_info(port=P)                   # bootloader version, board, SoftDevice, Last-Fault
nrf52_backup_firmware(port=P)                   # copy CURRENT.UF2 into the data dir
install(port=P, source={"file": "fw.uf2"})      # refuses a wrong-SoftDevice image
nrf52_factory_erase(port=P)                     # wipe saved data, keep the firmware
nrf52_mount_uf2(port=P)                         # mount the drive where the host does not automount
nrf52_eject_uf2(port=P)                         # unmount and eject it (asks first)
```

Pass the board's app port, preferably its `/dev/serial/by-id/` path. The drive is matched by USB serial, and the by-id name still carries that serial after the link disappears in UF2 mode. The tools read the drive where the OS mounts it, so on a host that does not automount, mount it first. `nrf52_factory_erase` needs a bootloader whose `INFO_UF2.TXT` has a `Factory-Erase:` line. A bootloader self-update (`update-*.uf2`) installs the same way; it asks even with `confirm=True` under `MESHTASTIC_MCP_CONFIRM=human`.

Serial DFU still works: `install(port, source={"file": "fw.zip"})` does the 1200 baud touch itself, dropping DTR as the nRF52 core needs, and fails unless adafruit-nrfutil printed `Device programmed.`. A board left sitting in serial DFU takes the package with no touch:

```text
vendor(tool="nrfutil", args=["dfu", "serial", "--package", "fw.zip", "--port", "/dev/ttyACM0",
                             "-b", "115200", "--singlebank"], confirm=True)
```

A board whose USB serial has wedged fails the call within its timeout with a hint to power-cycle it; `uhubctl_cycle` on its hub port brings it back.

### Headless Linux bench

Flashing needs no mount or eject. `nrf52_mount_uf2` and `nrf52_eject_uf2` exist for benches that test the bootloader's eject handling or run with no desktop to automount the drive. On a desktop session and on macOS they need no setup. Over SSH or from a systemd service, udisks2 refuses them until polkit allows it; the refusal prints a ready-to-run `sudo tee /etc/polkit-1/rules.d/...` command with only the actions that call needs, and `doctor` reports `udisks` as degraded with the same command.
