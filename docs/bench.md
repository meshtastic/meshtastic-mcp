# Running a shared bench

A bench is one host with radios attached and several agents or people using them. This page covers what keeps them from colliding. Setting up the hardware and the tiered test suite is in [bench-setup.md](bench-setup.md).

## One server per host

`meshtastic-mcp serve` owns the serial ports, the recorder and the firmware tree for the whole host. It writes a lock in the data dir, and a plain `meshtastic-mcp` started on the same host finds it and bridges to it over stdio, so every client goes through one process. `MESHTASTIC_MCP_STANDALONE=1` runs a client on its own instead. Running `serve`, its token and its systemd unit are in [serve.md](serve.md).

When the server runs as a systemd unit, its `PATH` comes from the unit, not your login shell. Put `/usr/sbin` and the PlatformIO, Android and Homebrew directories in it, or tools like `uhubctl` look missing.

## Names, leases and calls

- Each radio gets a stable name, `<model>-<last four hex of its node id>`, the first time a connection reaches it. Every tool takes the name in place of a port, and it follows the board across re-enumeration. `devices()` lists them without opening a port. Rename one with `meshtastic-mcp devices --name`.
- `claim` takes a radio for one session. Another session's device calls on it are refused until `release`, or until the lease runs out. A lease follows the board into its bootloader. Pass the handle `claim` returned as `lease` on your own calls. A person can list leases with `meshtastic-mcp leases` and end one with `meshtastic-mcp leases --break <name>`.
- One call per serial port runs at a time. A second call fails at once with a "busy" error rather than queuing, so retry it.
- Anything longer than a minute runs as a job. The tool returns a `job_id`, and `job` follows it.

## Checking the host

`meshtastic-mcp doctor`, or the `doctor` tool, checks every prerequisite and prints the command that fixes each gap. On a bench it also reports:

- `serve`: whether this host's `serve` runs the code in the checkout. A server started before the last pull shows as degraded, with the restart command.
- `install-source`: whether the installed package tracks the checkout. An editable install does.
- A binary found outside the server's `PATH` shows as degraded, not missing, with the fix.
- `udisks` (Linux only): whether this user may mount and eject a UF2 drive.

### Headless Linux

A remote shell is not the desktop's active session, so polkit refuses udisks mount and eject from it. Flashing does not need either; only `nrf52_mount_uf2` and `nrf52_eject_uf2` do. When udisks refuses, the tool prints the one-shot `sudo tee` command that grants exactly the actions it needs. Desktop Linux and macOS need no setup.

## Reading the server log

Every tool call leaves one line in the server's log: the tool, its target, the client, how long it took, and for a failure, why. Argument values are never logged, only their names. On a systemd bench:

```bash
journalctl --user -u meshtastic-mcp.service --since -1h -o cat | grep meshtastic_mcp.calls
```
