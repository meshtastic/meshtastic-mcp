# Safety

An agent with this server can reboot, erase and reflash real radios, and it reads text that strangers wrote. This page covers the controls: the confirmation gate, leases, read-only mode, read-back on config writes, long-running jobs and untrusted input.

## The confirmation gate

Every tool that can reboot, erase, reflash or rewrite a board takes a `confirm` argument. An agent can pass `confirm=True` for itself, so on its own it is not a human gate. The server asks the human instead whenever the client can show a prompt.

| Call | Client declared elicitation | Client cannot ask |
| --- | --- | --- |
| `confirm` omitted or `False` | The human sees `Allow: <action>?`. Accept runs the tool; decline fails it and nothing runs. | Fails with `<action> is destructive and requires confirm=True`. |
| `confirm=True` | Runs. | Runs. |
| `confirm=True` on an irreversible tool with `MESHTASTIC_MCP_CONFIRM=human` | The human is asked anyway. | Fails with `<action> needs a human to confirm it`. |

`MESHTASTIC_MCP_CONFIRM` takes `agent` (the default) or `human`; any other value fails the gated call. A subagent or `claude -p` session cannot ask, so it keeps the `confirm=True` contract.

The gate runs before the tool takes a port lock or starts a job. An answer counts only for the prompt it was given for: a reply sent unasked, or a call retried with different arguments, is asked again.

### Which actions are gated

- Asks for: `reboot`, `shutdown`, `inject_frame`, `pa_sweep`, `uhubctl_power` (`off` only), `uhubctl_cycle`, `push_fake_nodedb` (`target="hardware"` only), `pio_flash`, `flash_start`, `update_flash`, `nrf52_enter_uf2`, `nrf52_flash_uf2`.
- Irreversible, so `human` policy asks even with `confirm=True`: `factory_reset`, `erase_and_flash`, `vanity_apply`, `atak_fleet_down` with `delete_clones=True`, `nrf52_factory_erase`, and the destructive `esptool_*`, `nrfutil_*` and `picotool_*` calls.

The vendor tools look at their subcommand first. `esptool_raw(args=["flash_id"])` never asks; `esptool_raw(args=["erase_flash"])` does. Only a short list of read-only subcommands runs without asking (esptool `chip_id`, `flash_id`, `read_mac` and the like, nrfutil `version` and `device list`, picotool `info`); any subcommand the list does not name asks, and counts as irreversible under `human` policy.

### Over HTTP

With `meshtastic-mcp serve`, each client declares its own capabilities. A client that says it supports elicitation gets the prompt, but nothing proves a person answered it, so over HTTP the prompt is not a human gate. The bearer token decides who can call the tools at all, and anyone holding it can run every tool the server exposes. See [serve.md](serve.md).

### For agents

A refusal means the client could not ask. Tell the user what the tool would do and pass `confirm=True` only after they agree. Do not retry with `confirm=True` on your own.

## Leases

The bench is shared, and a lease gives one session a radio for a while. `claim(port)` returns a `lease` handle; pass it to every device tool on that radio, and call `release(lease)` when done. While a lease is live, a device call without the handle is refused with a message that names the holder and the seconds left, for example `<port> is leased to <holder> on <host> for another 312s`.

- A lease lasts `ttl_s` seconds (default 600, at most 3600). Claim again with the `lease` handle to extend it.
- `holder` is a short label shown to sessions that are refused.
- A lease is a file in the data dir (`MESHTASTIC_MCP_DATA_DIR`), so every server on the host sees it: stdio servers from different sessions and one `serve` process alike.
- A lease follows the board, not its port. It is keyed on the USB identity, so it survives a reboot, DFU mode or a replug that renames the port.
- A job started under a lease keeps it until the job ends.
- Only the TTL ends a lease nobody releases, and only a person breaks one early: `meshtastic-mcp leases` lists the live leases on the host, and `meshtastic-mcp leases --break <port or device>` ends one.

`configure` takes a lease of its own for the length of the call (120 seconds, held as `configure`) when you pass none, so no other session writes to the radio mid-change. It does not do this for `dry_run` or a `ble:` node.

## Read-only mode

`MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state. A hidden tool is not listed and cannot be called, so an agent in read-only mode has no write tool to misuse. Set it in the server's environment: under `serve` it shapes the tool list for every client. See [toolsets.md](toolsets.md).

## Config writes are read back

`configure` does not trust the firmware's acknowledgement, because the firmware acknowledges writes it drops. Before writing, it saves the current config as a snapshot and returns the snapshot's name, so `restore=<name>` puts it back. After writing, it reads every change back over a fresh connection:

- `verified` is `true` only when every change stuck.
- `mismatches` names a requested value that did not stick, with what the radio holds instead.
- `side_effects` lists other fields in the sections written that the firmware changed on its own, such as a region change capping `tx_power`. A side effect is not a failure.
- `dry_run=True` reports the changes and writes nothing.

A radio's own keys are never restored from a snapshot.

## Jobs and long-running work

An MCP client usually gives up on a call after about 60 seconds. A PlatformIO build or a serial upload takes minutes, so this server runs that work as a job and returns a `job_id` to follow.

A job runs in the server process and keeps a log under the data dir. Its id is good until the server restarts, because jobs are not saved. Under `serve`, every client shares one job registry, so a `job_id` one client started can be followed from another.

| Start | Follow | Stop |
| --- | --- | --- |
| `build_start` | `build_poll(build_id=...)` | cancel the `build` task |
| `flash_start` | `flash_poll(job_id=...)` | cannot be stopped mid-write |
| `vanity_grind_start` | `vanity_grind_poll` | `vanity_grind_stop` |
| `ble_sniff_start` | `ble_sniff_poll` | `ble_sniff_stop` |
| `drive_display_start` | `drive_display_poll` | |
| `screenshot_set_start` | `screenshot_set_poll` | |

The `job(id)` tool follows any job by its id. Poll every few seconds until `status` is `done` or `failed`; a poll returns the exit code, the artifacts and the tail of the log.

### A flash cannot be canceled

`build`, `pio_flash`, `erase_and_flash`, `update_flash` and `nrf52_flash_uf2` start a job and wait for it in the same call. Their result carries the `job_id`, and a failure is a tool error with the tail of the output.

Before it waits, each flash tool logs a line to the client naming its job, for example `flashes job 3f2a... started for heltec-v3; ... flash_poll(job_id='3f2a...') follows it`. A flash keeps writing after its call is canceled or times out. Follow that job to the end before you reset, unplug or reflash the board. `build`'s job is the same one `build_start` makes, so `build_poll(build_id=<job_id>)` reads it.

### Progress

While they wait, these five tools send MCP progress notifications to a client that asked for them with a progress token. The esptool, nrfutil and picotool tools (`esptool_raw`, `nrfutil_dfu`, `picotool_load` and the rest) send them while they run.

- esptool's write counts bytes over the images esptool has named a flash range for, so the total grows when the next image starts.
- picotool's bar counts percent.
- adafruit-nrfutil prints no total, so its notifications count DFU packets.
- A UF2 copy counts bytes copied. The drive receives them during the sync that follows, and the message says so.
- A compile has nothing to count, so its notifications carry the last line of output and no total.

A caller gets at most one notification a second however fast a build prints, and the progress value only grows. In a background task the same updates show as the task's status message. A client that sends no progress token gets no notifications and the same result.

### MCP background tasks

Install the `[tasks]` extra to let those five tools run as MCP background tasks:

```bash
uv tool install 'meshtastic-mcp[tasks]'
```

A client that supports tasks gets a task handle back at once and collects the result later. Other clients, and servers without the extra, run the tools inline: the call blocks until the job ends, so those clients should prefer `build_start` and `flash_start` with their poll tools.

Canceling a `build` task stops the build and its whole process group. Canceling a flash task leaves the write running; follow it with `flash_poll`. An inline `build` that a client abandons on a timeout keeps building, and only an explicit task cancel stops it. A build that runs past its 15 minute limit is killed with every process it started.

A gated tool inside a task still asks for confirmation, through the task's `input_required` state, so an irreversible flash can be confirmed there.

### Choosing

- The client runs tasks: call `build` or `pio_flash` and wait for the task.
- It does not: call `build_start` or `flash_start`, then poll.
- A call failed or was canceled after it started a job: find the job id in the log line or the result, and poll it.

## Untrusted input

Some tools return text that a stranger controls, and an agent that reads it can be steered by instructions hidden in it:

- Mesh traffic: `logs_window`, `packets_window` and the node and message fields of `inspect` carry names and text written by any node on the mesh.
- App surfaces: `android_ui_dump`, `android_screenshot` and `android_read_logcat` echo the same node names and messages one hop removed.
- TAK peers: `cot_relay_status` returns callsigns supplied by connected TAK clients.
- Discord: every `discord_*` message is public text from anyone on the server. The per-message `trust` tier is a reading aid, not authentication.
- BLE: `ble_sniff_poll` returns device names chosen by whoever owns any radio in range, so an attacker needs proximity and nothing else.

The risk is the combination of private data (config, channel URLs, the node list), untrusted content and a way out (`send` broadcasts on the mesh). Do not read untrusted mesh content and send messages in the same task without a human reviewing the step in between. Use `MESHTASTIC_MCP_READ_ONLY=1` for sessions that only need to look. [SECURITY.md](../SECURITY.md) covers reporting a vulnerability and the mitigations in place.
