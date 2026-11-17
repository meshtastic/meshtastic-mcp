# Confirming destructive tools

Every tool that can reboot, erase, reflash or rewrite a board takes a `confirm` argument. An agent can pass `confirm=True` for itself, so on its own it is not a human gate. The server asks the human instead whenever the client can show a prompt.

## How a gated call is decided

| Call | Client declared elicitation | Client cannot ask |
| --- | --- | --- |
| `confirm` omitted or `False` | The human sees `Allow: <action>?`. Accept runs the tool; decline fails it and nothing runs. | Fails with `<action> is destructive and requires confirm=True`. |
| `confirm=True` | Runs. | Runs. |
| `confirm=True` on an irreversible tool with `MESHTASTIC_MCP_CONFIRM=human` | The human is asked anyway. | Fails with `<action> needs a human to confirm it`. |

`MESHTASTIC_MCP_CONFIRM` takes `agent` (the default) or `human`; any other value fails the gated call. A subagent or `claude -p` session cannot ask, so it keeps the `confirm=True` contract.

The gate runs before the tool takes a port lock or starts a job. An answer counts only for the prompt it was given for: a reply sent unasked, or a call retried with different arguments, is asked again.

## Gated tools

- Asks for: `reboot`, `shutdown`, `inject_frame`, `pa_sweep`, `uhubctl_power` (`off` only), `uhubctl_cycle`, `push_fake_nodedb` (`target="hardware"` only), `pio_flash`, `flash_start`, `update_flash`, `nrf52_enter_uf2`, `nrf52_flash_uf2`.
- Irreversible, so `human` policy asks even with `confirm=True`: `factory_reset`, `erase_and_flash`, `vanity_apply`, `atak_fleet_down` with `delete_clones=True`, `nrf52_factory_erase`, and the destructive `esptool_*`, `nrfutil_*` and `picotool_*` calls.

The vendor tools look at their subcommand first. `esptool_raw(args=["flash_id"])` never asks; `esptool_raw(args=["erase_flash"])` does. The check reads every non-flag argument, so `["--port", "/dev/x", "erase_flash"]` is caught too. Under `human` policy every destructive vendor call asks.

## Background tasks

A gated tool that runs as an MCP background task (see [background-jobs.md](background-jobs.md)) asks through the task's `input_required` state, so an irreversible flash can still be confirmed there.

## Over HTTP

With `meshtastic-mcp serve`, each client declares its own capabilities. A client that says it supports elicitation gets the prompt, but nothing proves a person answered it, so over HTTP the prompt is not a human gate. The bearer token decides who can call the tools at all; see [serve.md](serve.md).

## For agents

A refusal means the client could not ask. Tell the user what the tool would do and pass `confirm=True` only after they agree. Do not retry with `confirm=True` on your own.
