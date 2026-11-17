# Long-running work: jobs and tasks

An MCP client usually gives up on a call after about 60 seconds. A PlatformIO build or a serial upload takes minutes, so this server runs that work as a job and gives you a `job_id` to follow it.

## Jobs

A job runs in the server process and keeps a log under the data dir. Its id is good until the server restarts; jobs are not saved. Under `meshtastic-mcp serve` every client shares one job registry.

| Start | Follow | Stop |
| --- | --- | --- |
| `build_start` | `build_poll(build_id=...)` | cancel the `build` task (below) |
| `flash_start` | `flash_poll(job_id=...)` | cannot be stopped mid-write |
| `vanity_grind_start` | `vanity_grind_poll` | `vanity_grind_stop` |
| `ble_sniff_start` | `ble_sniff_poll` | `ble_sniff_stop` |
| `drive_display_start` | `drive_display_poll` | |
| `screenshot_set_start` | `screenshot_set_poll` | |

Poll every few seconds until `status` is `done` or `failed`. A poll returns the exit code, the artifacts and the tail of the log.

## Tools that wait for their own job

`build`, `pio_flash`, `erase_and_flash`, `update_flash` and `nrf52_flash_uf2` start a job and wait for it in the same call. Their result carries the `job_id`, and a failure is a tool error with the tail of the output.

Before it waits, each flash tool logs a line to the client naming its job, for example `flashes job 3f2a... started for heltec-v3; ... flash_poll(job_id='3f2a...') follows it`. A flash cannot be canceled, so if the call times out or is canceled, that id is how you follow the write to the end. `build`'s job is the same one `build_start` would make, so `build_poll(build_id=<job_id>)` reads it.

## Progress

While they wait, these five tools send MCP progress notifications to a client that asked for them with a progress token. The esptool, nrfutil and picotool tools (`esptool_raw`, `nrfutil_dfu`, `picotool_load` and the rest) send them while they run.

- esptool's write counts bytes over the images esptool has named a flash range for, so the total grows when the next image starts.
- picotool's bar counts percent.
- adafruit-nrfutil prints no total, so its notifications count DFU packets.
- A UF2 copy counts bytes copied. The drive receives them during the sync that follows, and the message says so.
- A compile has nothing to count, so its notifications carry the last line of output and no total.

A caller gets at most one notification a second however fast a build prints, and the progress value only grows. In a background task the same updates show as the task's status message. A client that sends no progress token gets no notifications and the same result.

## MCP background tasks

Install the `[tasks]` extra to let those five tools run as MCP background tasks:

```bash
uv tool install 'meshtastic-mcp[tasks]'
```

A client that supports tasks gets a task handle back at once and collects the result later. Other clients, and servers without the extra, run the tools inline: the call blocks until the job ends, so for those prefer `build_start` and `flash_start` with their poll tools.

Canceling a `build` task stops the build and its whole process group. Canceling a flash task leaves the write running; follow it with `flash_poll`. An inline `build` that a client abandons on a timeout keeps building, and only an explicit task cancel stops it. A build that runs past its 15 minute limit is killed with every process it started.

A gated tool inside a task still asks for confirmation; see [confirmations.md](confirmations.md).

## Choosing

- The client runs tasks: call `build` or `pio_flash` and wait for the task.
- It does not: call `build_start` or `flash_start`, then poll.
- A call failed or was canceled after it started a job: find the job id in the log line or the result, and poll it.
