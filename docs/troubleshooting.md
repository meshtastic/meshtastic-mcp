# Troubleshooting

Each entry gives the symptom, the cause and the fix. `meshtastic-mcp doctor` is the first thing to run when a tool fails and the cause is not obvious.

The server logs one line per tool call to stderr: the tool, its target radio, the duration and, on failure, the reason a client saw. Under `serve` on a systemd host, read it with `journalctl --user -u meshtastic-mcp.service`; under a stdio client, it is in the client's MCP server log.

## A port is busy

Symptom: a tool fails with `Port <port> is busy: another device operation is in flight. Retry shortly.`

Cause: the server allows one call per serial port at a time. A second call fails at once instead of queuing, so a poll loop or two agents on one radio collide.

Fix: retry after a few seconds. Do not poll `inspect` in a tight loop; read the recorder with `query` or `wait_for` for ongoing observation. If the same port stays busy, another process may hold it, such as FleetSuite (its monitors own the bench's ports while it runs) or a terminal monitor.

## A lease is held by another session

Symptom: a device call fails with `<port> is leased to <holder> on <host> for another <N>s. Use another device, or wait for the lease to end.`

Cause: another session called `claim` on that radio, and a device call without its lease handle is refused. A lease follows the board, so a new port name does not get around it.

Fix: use another radio, or wait for the seconds named in the message. If the holder is gone, a person can list leases with `meshtastic-mcp leases` and end one with `meshtastic-mcp leases --break <port or device>`. If you are the holder, pass the handle `claim` returned to each device call, and `release` it when done. See [safety.md](safety.md).

## "did not return within 18s ... looks wedged"

Symptom: `Opening <port> did not return within 18s; the board's USB serial looks wedged. Power-cycle it (uhubctl_cycle) and retry.` A later call on the same port fails with `still stuck in an earlier open that never returned`.

Cause: the board's USB serial stopped answering and the open call never returned, which happens on an nRF52 after rapid open and close cycles. The server gives up after the call's timeout plus a 10 second grace, and refuses new opens on that port until the stuck one ends.

Fix: power-cycle the board's hub port with `uhubctl_cycle(role=...)` or `uhubctl_cycle(location=..., port=...)` (names from `uhubctl_list`), or replug it, then retry. A hub that cannot switch port power needs a manual replug.

A radio that restarts at the moment a client connects can miss the client's config request. The server resends the request every 3 seconds while nothing has answered, so this case recovers on its own instead of waiting out the full timeout. If a call still fails after that, the radio is not answering at all, and the entry above applies.

## A radio held over BLE refuses a serial open

Symptom: a call on a radio's serial port fails with "is held over BLE (...) in this server, and a serial open would turn its BLE off", or a `ble:` call fails with "is open over serial".

Cause: a serial connection turns an ESP32 radio's BLE off, so the server refuses a serial open of a radio it holds over BLE, and a BLE connect to one it holds over serial. The radio's BLE identity comes from the device registry, which records it after the first BLE connect.

Fix: wait for the other connection to close, or use the transport already open. `node="ble:<name or address>"` or `ble:<device name>` reaches a radio over BLE through meshtastic-python; `MESHTASTIC_MCP_BACKEND=sdk` uses the Kotlin SDK CLI instead.

## Unknown tool

Symptom: the client reports `Unknown tool: <name>`, or a tool in these docs is not in the tool list.

Cause: the tool is hidden. Three settings hide tools:

- `MESHTASTIC_MCP_TOOLSETS` names the groups to expose, and a tool outside them is not listed. A `-group` entry removes one.
- `MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state.
- The `legacy` toolset (`device_info`, `get_config`, `set_config`, `send_text`, `reboot` and the other pre-1.0 tools) is opt-in. It comes only when named: `MESHTASTIC_MCP_TOOLSETS=all,legacy`. A call to a legacy name while it is hidden fails with the call that replaces it, and a `discord_*` or local-model name says where it went.

A tool can also be missing because its capability is not active, for example the firmware tools without `MESHTASTIC_FIRMWARE_ROOT`, or the Android tools without `adb`.

Fix: check the server's environment, and under `serve` check the server process, not the client. Run `doctor` for a missing prerequisite, and see [toolsets.md](toolsets.md). Capabilities are detected once at startup, so restart the server after fixing a prerequisite.

## A prerequisite is missing

Symptom: a tool fails because `pio`, `adb`, `xcrun`, `uhubctl` or another binary is not found, or a source root is unset.

Fix: run `meshtastic-mcp doctor`. It reports each dependency as `ok`, `missing` or `degraded` and prints the install command for the host. `doctor --json` returns a `fix_commands` list that is ready to run, and the `doctor` tool gives an agent the same report. `meshtastic-mcp provision` clones the firmware, Android and Apple repositories. See [install.md](install.md).

A binary that exists but is off the server's PATH shows as `degraded` with "found at <path> but not on this server's PATH". A systemd unit's default PATH has no `sbin`, which hides `uhubctl`; add its directory with an `Environment=PATH=` drop-in. `doctor` also flags a running `serve` on other code than this install (`serve`), and a git or PyPI install next to a `$MESHTASTIC_WORKSPACE/meshtastic-mcp` checkout at another commit (`install-source`); reinstall editable from the checkout and restart the server.

## The UF2 drive is not mounted on a headless host

Symptom: `nrf52_bootloader_info`, `nrf52_backup_firmware` or `install` with a `.uf2` cannot find the board's drive, although the board is in UF2 mode.

Cause: the tools read the drive where the operating system mounts it. A desktop automounts it, and a headless host does not. On Linux the tools look under `/run/media/<user>`, `/media/<user>`, `/media` and `/mnt`; on macOS under `/Volumes`.

Fix: `nrf52_mount_uf2(port=...)` mounts it through udisks2 (Linux) or `diskutil` (macOS), or mount it by hand under one of those directories, for example `sudo mount /dev/disk/by-label/<label> /mnt/uf2`, then retry. Pass the board's app port, preferably its `/dev/serial/by-id/` path: the drive is matched by USB serial, and the by-id name still carries that serial after the link disappears in UF2 mode. The board stays in UF2 mode until a UF2 lands or it is power-cycled.

## Mounting or ejecting a UF2 drive is refused over SSH

Symptom: `nrf52_mount_uf2` or `nrf52_eject_uf2` fails with "polkit refused".

Cause: udisks2 lets only the desktop session mount or eject a drive; an SSH session or a systemd service is refused. Flashing never needs either tool.

Fix: run the one-shot command the error prints, which grants only the actions that call needs. `doctor` reports the same under `udisks`. See [firmware-builds.md](firmware-builds.md#headless-linux-bench).

## `configure` returns `verified: false`

Symptom: a `configure` result has `verified: false` with `mismatches`, or a `side_effects` block next to `verified: true`.

Cause and fix depend on which key is present:

- `mismatches` names a value you asked for that the radio does not hold after the write, as `{wanted, read_back}`. The firmware acknowledged a write it dropped, or changed the value. Check the value against the field's type and enum names, read the radio with `inspect`, and try again. `restore=<snapshot>` returns the radio to its config from before the call. `verified` is `false` exactly when `mismatches` is not empty, or when the read-back itself failed (see `errors`).
- `side_effects` lists other fields in the sections you wrote that the firmware changed on its own, for example `tx_power` capped after a region change. They are not failures and do not make `verified` false. Check that the new values suit you.

A radio restarts for most config sections, so a result with `restarted: true` is normal. If the call fails on a restart that takes too long, raise `timeout_s`.

## A sent message does not show up in the packet stream

Symptom: a message went out, and `query` over the recorder's packets shows nothing for it.

Cause: a node never sees its own transmission on the receive path. The library drops the firmware's echo of a packet the node sent, so the stream stays empty even when the message was delivered.

Fix: do not confirm a local send with the packet stream. Watch the firmware's own `Started Tx` log line instead, which reaches the recorder only while the radio's debug log API is on (`security.debug_log_api_enabled` through `configure`). For end-to-end delivery, send a direct message with `want_ack=True`, or observe the packet on a second node. `send(..., wait_for_tx=True)` reports `tx_confirmed` as `true` (seen), `false` (logs flowed and showed no TX) or `null` (not observable, which is not a failure).
