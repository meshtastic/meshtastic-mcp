# Dual-plane failure triage

When a loop reports `LOOP … FAIL`, the dual-plane design tells you *which* plane broke. This
pairs with the **`triage_e2e_failure` MCP prompt** (which seeds the same workflow with the
token + deadline) and the recorder windows.

## The four buckets

Align the device-plane wire truth with the app-plane UI at the same wall-clock. Every failure
falls into exactly one bucket:

| Bucket | Device plane says | App plane says | Likely cause |
|---|---|---|---|
| **never-sent** | no TX of the token from the tester | — | stimulus bug; tester not configured (region/UDP); wrong port |
| **sent-not-received** | tester TX present, DUT has no RX | — | mesh not formed (no NodeInfo warmup), multicast not looped (Darwin #75), region mismatch |
| **received-not-rendered** | DUT RX of `TEXT_MESSAGE_APP` w/ token | no bubble | app not connected/subscribed, wrong screen, app dropped the packet |
| **rendered-not-asserted** | DUT RX present | bubble *is* on screen | oracle bug: polling the wrong screen, a11y tree empty (use the vision oracle), token typo |

## Procedure

1. **Collect the window.** Around the assertion deadline:
   - `packets_window` — did a `TEXT_MESSAGE_APP` carrying the token cross the wire? from which node?
   - `logs_window` — NAK / `err=5` MAX_RETRANSMIT (expected on broadcast in a flat mesh — not a
     delivery failure; see `loop-outbound.md`)? config-reboot loops? bind errors?
   - `events_window` — your `mark_event` anchors.
   - app `layout` / `screen capture` at the deadline — connection state + current screen.
   The `triage_bundle` MCP tool gathers the three recorder windows in one call.
2. **Correlate by epoch timestamp.** All recorder rows and `android layout` snapshots share the
   same clock — that alignment is the whole point.
3. **Classify** into one bucket above. The bucket dictates the fix:
   - never/sent-not-received → device plane (configure region+UDP, warm up NodeInfo, check #75 on macOS).
   - received-not-rendered → app plane (re-run the connect journey; confirm Primary Channel).
   - rendered-not-asserted → oracle (vision oracle if the a11y tree is empty; check the token).
4. **Report.** One line: `root-cause=<bucket>: <detail>` + the minimal repro (which single step,
   which port, which token). Attach the packets_window tail + the app screenshot at the deadline.

## Fast checks

- Mesh formed? `list_nodes` on the DUT should show the tester's short name. If only itself, the
  mesh didn't form — NodeInfo warmup or multicast loopback.
- App connected? The Connect screen shows "Subscribed"/"Connected Radio". If "Disconnected:
  Error while receiving data", the device plane went away (nodes died) — check the supervisor.
