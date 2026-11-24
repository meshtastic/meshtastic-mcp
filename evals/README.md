# evals

A fixed task set that scores the MCP server the way an agent uses it. Every change on `next` is
measured against `baseline.json`; no phase merges while a run scores below it.

`tasks.json` holds 23 tasks: 16 `dev` and 7 `holdout`. Tune the tools against `dev` only, and run
`holdout` to check the tuning did not overfit. Each task has a prompt, a `check` on the agent's
final answer, and, where the task changes a radio, a `verify` command that reads the radio back and
a `teardown` that restores it. A `setup` command runs before the agent, for a task that needs a
precondition such as another holder's lease; its `teardown` runs even when setup fails.
A check fails an answer whose opening paragraph admits failure ("I couldn't find it"); the
same words in a side note further down do not. `check.forbid_tools` fails a run that called a
matching tool, by bare name, so a task can require the one-call path.

`install-tree-build`, `build-as-job`, `vendor-*`, `sim-inject-status`, `leased-radio-respected`,
`traceroute-between-radios` and `remote-telemetry-request` cover the tools the first baseline never
ran. The build and install tasks need the `firmware` toolset (`MESHTASTIC_MCP_TOOLSETS=all`) and
`MESHTASTIC_FIRMWARE_ROOT` in the server's environment, and `leased-radio-respected` needs
`mcp_python` in `devices.json`: the interpreter that imports this package on the bench host. There is
no `app` task, because james-pc has no Android emulator running, and no remote admin (`dest`) task,
because the bench radios do not hold each other's admin keys.

| `risk` | Runs when |
| --- | --- |
| `read-only` | always |
| `mutating` | `--allow-mutating` (changes config, reboots, injects) |
| `destructive` | `--allow-destructive` (flashes or recovers a board) |

Two tasks have weak checks (`boot-panic-in-logs`, `waypoint-in-replay-sim`): they score tool use and
errors, not correctness, and their `notes` say so. `ble-connect-list-nodes` is expected to fail on
`master`, which has no BLE transport.

## Running

Each task runs `claude -p --mcp-config <file> --strict-mcp-config` with only that server's tools
allowed. The runner reads the stream-json events and records pass or fail, tool calls by name, tool
errors, tokens, cost, turns and wall time.

```bash
python -m evals.runner --list
python -m evals.runner --devices devices.json --mcp-config bench.mcp.json --label master
python -m evals.runner --devices devices.json --mcp-config bench.mcp.json --label master --allow-mutating
```

`devices.json` names the radios and ports the prompts use; see `devices.example.json`. Hardware tasks
run on the james-pc bench. The bench is shared, so check for other sessions' servers first, and
resolve ports by `/dev/serial/by-id/`. The runner sets `MESHTASTIC_MCP_STANDALONE=1`, so the
server in `--mcp-config` runs as its own process instead of bridging to the bench's `serve`: a
run measures that code, and for its duration it is a second owner of the radios.

`--repeat N` runs every chosen task N times. The report's `per_task` gives each task's pass rate
and the tool calls and input tokens of every run, so a flip between runs shows as a rate below 1.

Results go to `evals/results/<label>-<UTC stamp>.json` (not committed), one file per invocation,
so a later run never overwrites an earlier one. Each result keeps the first 2,000 characters of
the agent's answer, so a failed check can be judged by hand.

## Baseline

`baseline.json` is `master` at `3b3792d` on the james-pc bench: 10 of 11 scored tasks pass, at
$1.34 and 88 tool calls for the 12 tasks run. The destructive tasks and the emulator-dependent
waypoint task were not run. `telemetry-last-hour` fails because `telemetry_timeline` does not
say which node a reading came from, so the agent cannot separate the radio's own battery from
relayed ones.

It is one run per task. The same task can pass in one run and fail the next: earlier runs gave
`telemetry-last-hour` both outcomes. Compare a change against the baseline task by task, and
rerun a task before calling a single flip a regression.
