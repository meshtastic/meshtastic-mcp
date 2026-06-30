# Journey-driven UI (the self-healing app plane)

The brittle part of e2e is the **app plane** — hardcoded tap coordinates and exact label
matches break on every app redesign (the `ci_apple_app_loop.py` tab-bar coordinate saga is
the cautionary tale). The robust alternative is a **journey**: a natural-language UI test the
*agent* executes against the live accessibility tree, deciding each tap from the goal, not a
script. The device plane (`mesh_up()` + the recorder) stays the deterministic oracle.

## What a journey is

An ordered list of natural-language `<action>`s the agent performs and verifies, one at a
time, against the running app. See the `android-cli` skill `references/journeys.md` for the
exact XML grammar and the `android` CLI's evaluation contract. Apple has no first-party journey
runner, but the **same journey XML drives the iOS Simulator** when the agent uses
`apple_sim.ui_dump`/`tap`/`type_text` as its senses instead of `android layout`.

Shipped journeys (in `references/journeys/`):
- `inbound.journey.xml` — connect over TCP, open Primary Channel, verify the token bubble.
- `outbound.journey.xml` — compose + send from the app (assert wire truth on the device plane).
- `node-sync.journey.xml` — a beaconed node appears in the app's node list.

## How to run a journey (agent loop)

1. **Device plane up first.** Bring up the mesh (`ci_device_mesh_e2e.mesh_up` or `mesh_e2e.py`)
   and note the DUT TCP port. This is your stimulus + oracle.
2. **Read the journey.** Load the `.journey.xml`; execute each `<action>` in order.
3. **Sense → act → verify per action.** For each action: dump the UI (`android layout` /
   `apple_sim.ui_dump`), find the element by its *semantic* label (not coordinates), tap/type,
   then verify the action's stated postcondition. If an element genuinely isn't present, the
   journey fails — do not improvise around it (the journey XML is the source of truth).
4. **Permission/onboarding dialogs** are not journey steps — dismiss them opportunistically
   whenever they appear (grant location/notifications/Siri so features work). They can pop up
   mid-flow, including during the ~30s mesh startup.
5. **Marker token.** Where a journey references "the token", use a fresh `E2E-$(date +%s)` and
   keep the device-plane and app-plane sides agreed on it.
6. **Verdict.** Emit `LOOP <name> <PASS|FAIL> token=… latency=…` exactly as the scripted loops do,
   so journey runs and scripted runs report identically.

## Why this beats coordinates

- **Version-resilient:** a moved button or renamed tab is still found by intent.
- **Cross-platform:** one journey, two app planes (Android `android` CLI, Apple `apple_sim`).
- **Self-healing:** when a step fails, the agent has the live tree + a screenshot to adapt or to
  report a precise, human-readable failure ("no 'Connect' tab on screen") instead of a silent
  mis-tap at `(40, 832)`.

## When to still script

CI without an agent-in-the-loop (the `android-e2e`/`apple-e2e` jobs) uses the scripted helpers
for determinism. The journeys are for **agent-driven** runs (local dev, triage, exploratory) and
as the human-readable spec the scripts implement. Keep them in sync: the journey is the intent,
the script is one frozen execution of it.
