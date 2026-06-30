# Loop: config write-back (app → device)

Change a setting in the app; assert it persisted on the DUT radio (RAM *and* NVS).

**Stimulus:** app config screen toggles a value (region, role, channel name, hop limit…)  
**Oracle:** MCP `get_config` on the DUT reflects it; survives a `reboot` (NVS proof)

## Steps

```bash
DUT=<dut serial port>          # the radio the phone is connected to (if serial-shared, see note)
MCP="$MESHTASTIC_FIRMWARE_ROOT/mcp-server/.venv/bin/python"

# 1. Read baseline from the device.
$MCP -c "from meshtastic_mcp import admin,json; print(json.dumps(admin.get_config('lora','$DUT'),default=str))"

# 2. App stimulus: navigate to the setting and change it (e.g. LoRa > Hop limit 3 -> 5).
#    Use android layout to find the control; adb input to change it; confirm with layout --diff.

# 3. Device oracle: re-read and assert the new value landed.
$MCP -c "from meshtastic_mcp import admin,json; print(json.dumps(admin.get_config('lora','$DUT'),default=str))"

# 4. NVS persistence proof: reboot and re-read.
$MCP -c "from meshtastic_mcp import admin; admin.reboot('$DUT', confirm=True)"   # confirm-gated
sleep 20
$MCP -c "from meshtastic_mcp import admin,json; print(json.dumps(admin.get_config('lora','$DUT'),default=str))"
```

Verdict: `LOOP config-writeback PASS field=<x> value=<new> persisted=<yes|no>`.

## Important — port contention

If the phone is connected to the DUT over **serial**, the MCP `get_config` call and the app
**cannot hold the same serial port at once** (exclusive lock). Resolve by either:
- DUT over **BLE/TCP** to the app, serial free for MCP reads, **or**
- read config via the app's own UI as the oracle and use MCP only when the app is disconnected.

## What to test

- Round-trip a value the app owns: `lora.hop_limit`, `lora.region`, device `role`, a channel
  name / URL (`get_channel_url` / `set_channel_url`).
- Negative case: set an invalid value in the app → assert the device rejects/clamps it and the
  app surfaces the error (no silent divergence).

## Failure triage

- New value in app but old on device → write never reached the radio (admin auth, session
  passkey, `is_managed` scope) — check device logs (`logs_window`).
- Value correct after set but reverts after reboot → it stuck in RAM only, NVS write failed.
