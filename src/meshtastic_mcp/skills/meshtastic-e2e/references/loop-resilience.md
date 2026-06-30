# Loop: resilience / fault injection (uhubctl)

Power-cycle a radio mid-conversation; assert the mesh **and** the app recover. This is the
app-facing mirror of the firmware suite's `test_peer_offline_recovery`.

**Requires** a uhubctl-capable switched USB hub (e.g. the per-port USB switch) + `brew install uhubctl`.

## Scenarios

### A — peer drops, app shows offline→online
```bash
TESTER=/dev/cu.usbmodem101
# 1. establish a live exchange (run loop-inbound once so the app shows the tester node online)
# 2. identify the tester's hub port:
uhubctl                                  # note location e.g. 1-1.3, port 3
# 3. cut power, watch the app mark it offline:
$MCP -c "from meshtastic_mcp import uhubctl; uhubctl.power_off('1-1.3', 3)"
for t in $(seq 1 60); do
  android layout 2>/dev/null | grep -A3 "<tester name>" | grep -qi 'offline\|last heard' && { echo "offline detected ~${t}s"; break; }
  sleep 1
done
# 4. restore power; assert re-enumerate + back online + same node num:
$MCP -c "from meshtastic_mcp import uhubctl; uhubctl.power_on('1-1.3', 3)"
sleep 25
$MCP "$S" info "$TESTER"                  # same my_node_num as before = clean recovery
android layout | grep -qi "<tester name>" && echo "LOOP resilience PASS (recovered)"
```

### B — relay drops mid-route, message recovers
With a 3-node line (DUT — RELAY — TESTER via the USB switch), power-cycle RELAY while a
message is in flight; assert the app eventually shows delivery once the path heals (mesh
re-routes / retries). Use a marker token and the `loop-outbound` oracle on the far end.

### C — config survives hard reset
Cut power (not a clean reboot) and assert region/channel/modem-preset survive — the NVS
durability check from the firmware `recovery` tier, observed from the app's config screen.

## One-shot cycle helper

```bash
# uhubctl.cycle toggles off->on in one call:
$MCP -c "from meshtastic_mcp import uhubctl; uhubctl.cycle('1-1.3', 3)"
```
The module funcs are `power_on(location, port)`, `power_off(location, port)`,
`cycle(location, port, delay_s=2)` — no `confirm`/`on=` kwargs (the `confirm` gate lives on
the `uhubctl_power`/`uhubctl_cycle` *MCP tools*, not the module). To exercise the gate, call
the tool form instead: `uhubctl_power(action='off', location='1-1.3', port=3, confirm=True)`.
> Verify exact arg names: `grep -n 'def power_on\|def power_off\|def cycle' \
> "$(python -c 'import meshtastic_mcp.uhubctl as u; print(u.__file__)')"`

## Safety

Power operations are **confirm-gated** for a reason — never auto-approve. Pin roles to hub
ports (`MESHTASTIC_UHUBCTL_LOCATION_<ROLE>` / `_PORT_<ROLE>`) so a cycle targets the intended
radio when several share a VID. Don't power-cycle the DUT radio out from under an active BLE
bond unless that's the scenario you're testing.

## Failure triage

- App never marks offline → app liveness/last-heard threshold too lax, or it's reading a
  cached node. Cross-check `device_info`/`list_nodes` on a *different* radio for ground truth.
- Recovers on device but app stays offline → app reconnect/refresh bug.
- `my_node_num` changed after power restore → NVS/identity problem on the radio (not the app).
