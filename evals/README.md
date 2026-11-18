# evals

A fixed task set that scores the MCP server the way an agent uses it. Every change on `next` is
measured against `baseline.json`; no phase merges while a run scores below it.

`tasks.json` holds 15 tasks taken from recorded usage: 10 `dev` and 5 `holdout`. Tune the tools
against `dev` only, and run `holdout` to check the tuning did not overfit. Each task has a prompt, a
`check` on the agent's final answer, and, where the task changes a radio, a `verify` command that
reads the radio back and a `teardown` that restores it.

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
resolve ports by `/dev/serial/by-id/`.

Results go to `evals/results/<label>.json` (not committed). Copy the run for `master` to
`baseline.json` once it has run against the full fleet.
