# Firmware builds and flashes

The firmware tools register when `MESHTASTIC_FIRMWARE_ROOT` points at a firmware checkout and PlatformIO's `pio` is installed. They form the `firmware` toolset. How jobs, polling and MCP background tasks work is in [safety.md](safety.md#jobs-and-long-running-work); how the confirmation gate works is in [safety.md](safety.md#the-confirmation-gate).

## One tree for every session

`MESHTASTIC_FIRMWARE_ROOT` is one checkout, set the same way for every session and worktree on the machine, so concurrent agents build in one `.pio` tree. PlatformIO rewrites `project.checksum` and cleans `.pio/build/*` when the env changes, so another session's build silently deletes yours, even between `build_poll` returning `done` and the next `ls`. Treat a finished build as perishable: flash it at once, and before a multi-device session check for other builders with `pgrep -af 'pio run'`.

## Building

`build` and `build_start` run `pio run -e <env>`. `build` returns `job_id`, `status`, `exit_code`, `duration_s`, the artifact paths under `.pio/build/<env>/`, the last 200 log lines, `worker_error` and the `build_flags` it used. A failed build is a tool error carrying the log tail.

| Argument | Effect |
| --- | --- |
| `with_manifest` | `True` (the default) adds the `mtjson` target, which writes an `.mt.json` manifest beside the firmware. |
| `userprefs` | A dict of `USERPREFS_<KEY>: value` baked into this build by injecting it into `userPrefs.jsonc`; the file is restored after the build. `userprefs_manifest` lists the keys; `userprefs_set` makes a lasting change. |
| `build_flags` | A dict of `-D<NAME>=<VALUE>` macros for this build only, passed through `PLATFORMIO_BUILD_FLAGS`. |

`build_flags={"DEBUG_HEAP": 1}` turns on per-thread leak detection and puts a `[heap N]` prefix on every log line. The recorder picks the prefix up and builds a high-resolution heap timeline, which `telemetry_timeline(field="free_heap")` reads beside the normal LocalStats packets (about one a minute). Pair it with `/leakhunt` to classify a leak.

`clean` runs `pio run -e <env> -t clean`, for a branch switch or a stale-cache build failure.

## Flashing

| Tool | What it writes |
| --- | --- |
| `pio_flash`, `flash_start` | `pio run -e <env> -t upload --upload-port <port>`, any architecture (ESP32, nRF52, RP2040, STM32). `port` is the board's serial port as `list_devices` reports it. |
| `erase_and_flash` | ESP32 only, through `bin/device-install.sh`: wipes the whole flash and writes the bootloader, app, OTA and LittleFS partitions from the `factory.bin`. The first flash of a blank ESP32 wants this. |
| `update_flash` | ESP32 only, through `bin/device-update.sh`: an OTA update of the application partition that keeps the device config and node database. Faster than `erase_and_flash`, but it cannot recover a broken bootloader. |

- `pio_flash` takes `userprefs`, baked in and restored after the upload, and `build_flags` for the rebuild it runs before uploading. Pass the flags to the flash: without them that rebuild relinks without the variable and silently drops them.
- `flash_start` returns `{job_id, status: "running", log_path}` within about a second, and a busy port fails there rather than on the first poll.
- `erase_and_flash` builds first when no `factory.bin` is present (`skip_build=True` requires a prior build instead) and rebuilds with the manifest target when the `factory.bin` has no `.mt.json` beside it. `userprefs` forces a rebuild, so it cannot be combined with `skip_build=True`; the file is restored after the upload.
- `update_flash` builds first if needed, and `userprefs` forces a rebuild there too.
- `flash_poll` follows a job from `flash_start`, or the `job_id` that `pio_flash`, `erase_and_flash`, `update_flash` and `nrf52_flash_uf2` return and log. It returns `status`, `elapsed_s`, `exit_code`, `error` when the cause is known, and the output tail.

For chip-level recovery, the vendor tools (`esptool_*`, `nrfutil_*`, `picotool_*`) are in the `hardware` toolset.

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

`userprefs_testing_profile` returns a userPrefs dict for an isolated test mesh (a private channel on a non-default slot, MQTT off), ready for the `userprefs` argument of `build`, `pio_flash`, `erase_and_flash` or `update_flash`. Give every device in a cluster the same `psk_seed`.

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
nrf52_flash_uf2(uf2_path="fw.uf2", port=P)      # refuses a non-UF2 or wrong-SoftDevice image
nrf52_factory_erase(port=P)                     # wipe saved data, keep the firmware
```

Pass the board's app port, preferably its `/dev/serial/by-id/` path. The drive is matched by USB serial, and the by-id name still carries that serial after the link disappears in UF2 mode. The tools read the drive where the OS mounts it, so on a host that does not automount, mount it first. `nrf52_factory_erase` needs a bootloader whose `INFO_UF2.TXT` has a `Factory-Erase:` line.

Serial DFU still works. `nrfutil_dfu(port, package_path)` does the 1200 baud touch itself, dropping DTR as the nRF52 core needs, and fails unless adafruit-nrfutil printed `Device programmed.`. A board left sitting in serial DFU takes the package with no touch:

```text
nrfutil_raw(args=["dfu", "serial", "--package", "fw.zip", "--port", "/dev/ttyACM0",
                  "-b", "115200", "--singlebank"], confirm=True)
```

A board whose USB serial has wedged fails the call within its timeout with a hint to power-cycle it; `uhubctl_cycle` on its hub port brings it back.
