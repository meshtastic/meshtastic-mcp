# Loop: node sync (device → app)

A node appears on the DUT radio; assert it shows up in the app's node list.

**Stimulus:** a real beacon from the TESTER, or `push_fake_nodedb` injected on the DUT  
**Oracle:** `android layout` over the app's Nodes screen shows the node's long/short name;
backstop with `list_nodes` on the DUT.

## Option A — real beacon (most faithful)

```bash
TESTER=/dev/cu.usbmodem101
# The tester broadcasts NodeInfo/position periodically. Force a fresh one by sending any
# broadcast, then wait for the DUT to learn it. Open the app's Nodes screen first.
$MCP "$S" send "$TESTER" "nodeinfo-warmup-$(date +%s)"
NAME="<tester long_name>"   # from: $MCP "$S" info $TESTER
for t in $(seq 1 45); do
  android layout 2>/dev/null | grep -q "$NAME" && { echo "LOOP node-sync PASS name=$NAME ~${t}s"; break; }
  sleep 1
done
```

## Option B — injected fake node (deterministic, no second radio)

`push_fake_nodedb` writes a synthetic node straight into the DUT radio's DB — the app sees
it as if it arrived over the air. Good for testing the node-list UI without RF.

```bash
DUT=<dut serial port or tcp>
# via the MCP tool (confirm-gated). Provide a unique node num + name token.
$MCP -c "
from meshtastic_mcp import server
# tool: push_fake_nodedb(size, target='portduino'|'hardware', port=<hardware only>, ...)
#       size is required and must be one of 250/500/1000/2000
# call the underlying impl with a unique short/long name token, e.g. 'E2E-NODE-1234'
"
# then assert in the app:
android layout | grep -q 'E2E-NODE-1234' && echo PASS || echo FAIL
```
> Check the exact `push_fake_nodedb` signature: `grep -n 'def push_fake_nodedb' \
> $MESHTASTIC_FIRMWARE_ROOT/mcp-server/src/meshtastic_mcp/server.py` and the impl it calls.

## Device backstop

Confirm the device truth independent of the app:
```bash
$MCP -c "from meshtastic_mcp import info,json; print(json.dumps(info.list_nodes('$DUT'),default=str))"
```
(Note: the `list_nodes` *summary* has been observed to drop names; read the raw DB via a
SerialInterface `iface.nodes` if you need long/short names reliably — see the main session log.)

## Failure triage

- Node in DUT `iface.nodes` but not in app → app node-list bug (filter, dedup, render).
- Node missing from the DUT DB too → it never arrived (RF range / channel) or the inject failed.
