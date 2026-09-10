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

## Running an Android journey with Artemis

For the **Android** app plane, hand the journey to Artemis instead of executing the
sense→act→verify loop inline. Artemis (`google/artemis`, MCP server `artemis`, clone at
`~/src/artemis`) is a purpose-built version of exactly the loop above: it reads the live
tree, decides each tap from the goal, and runs *out of your context* — every screenshot
and UI dump stays in its process instead of your window. Apple and desktop keep the
inline loop; the journey XML stays the shared source of truth.

```text
mobile_run_task(
  task_desc      = <the journey's <action> list, one numbered line each, + the token>,
  model          = "Flash",            # measured 70x cheaper than Pro; see Cost below
  locked_app_package = "com.geeksville.mesh.fdroid.debug",
  device_serial  = "<phone serial>",   # bind it; do not let it pick
)
```

`expected_output_desc` is Pro-only and is ignored on Flash — add it only when you have
already decided to pay for Pro.

Then poll `mobile_manage_task` at least once a minute (mobile tasks stall silently and the
completion wakeup is not reliable on its own), and read `stderr_log` on failure — that is
where Python tracebacks land.

### Cost: use Flash, not Pro

Measured on this journey, 2026-09-10, same phone and app:

| Profile | LLM calls | Prompt tokens | Wall clock |
|---|---|---|---|
| Flash | 3 | 24,032 | ~15 s |
| Pro | 97 | 1,669,659 (56% cached) | ~13 min |

**~70x for the same app steps.** Pro buys ADB shell, notes and a written report — and a
verdict you have to discard anyway (see below). Reach for Pro only when you need its
device probes for triage, never as the default journey runner. `expected_output_desc`
is Pro-only, which is the one real reason to pay.

### The hard rule: Artemis drives, the recorder decides

**Never take Artemis's verdict as the journey's verdict.** Its verification is
structurally fail-open — checked in `artemis/agents/checker/checker.py` on 2026-09-10:

- `verdicts_allow_release()` (line 110) counts **`inconclusive` as releasing**, and its
  own docstring at line 114 reads *"Assert failures never block release."*
- Line 433 downgrades a failed verdict with vague evidence to `inconclusive`, which then
  releases.
- Lines 442/457 give a check item that never got a verdict a default of `inconclusive`,
  which then releases.
- `verification_level: "strict"` is *"checkpoints with a larger repair budget"* — **more**
  improvisation before halting, not less.

So a run reports success when the Checker could not observe the thing it was checking. In
practice it does not always fail open — a Pro gate run on 2026-09-10 correctly failed an
assert on an impossible step rather than fabricating a pass — but the structure permits it,
and one honest run is not a guarantee. Treat `report_task_status`, `output.md` and every Checker verdict as a **hint**. The
`LOOP <name> <PASS|FAIL>` line in rule 6 above is still computed from the device plane —
the recorder, `packets_window`, `mesh_e2e.py` — exactly as for a scripted run. Nothing
about the oracle changes; only who does the tapping.

### Interleaving, per journey

- **`outbound`** — clean handoff. Every step is app-side, then the recorder asserts wire
  truth independently. This is the journey to trust first, because the device plane can
  check Artemis's self-report against reality.
- **`inbound`, `node-sync`** — the stimulus must land **after the app is connected**, and
  `mobile_run_task` is one-shot: it cannot wait for your signal mid-run. Nothing in this
  repo promises that the phone-API session replays a stimulus sent before the app
  subscribed, so do **not** simply inject first and hope it is buffered. Use the
  device-plane connect event as the handoff:

  1. Make the journey's last action a bounded `wait_for_text` on the token, using the
     hop-count deadline from `harness.md` rule 2.
  2. Start `mobile_run_task`; the journey connects the app as one of its own steps.
  3. Watch the device plane for the app's session — `replay_status` reporting
     `connected: true` with a client, or the recorder seeing the phone-API session open.
  4. Inject only then, while Artemis is inside that final bounded wait.

  `node-sync.journey.xml` permits seeding before *or* during the journey; step 3 is what
  makes "before" safe, so prefer it either way.

### Caveats

- Artemis is **Android-only**. One journey format, two runners.
- It resolves rule 3 above (*"do not improvise around it"*) toward self-healing — it will
  route around a missing element rather than fail. For interop testing that is usually
  right, but a journey run this way stops detecting UI regressions. Scripted helpers
  remain the CI path, as *When to still script* says.
- `--locked-app` / `locked_app_package` auto-launches the package. Bring the device plane
  up first so the app has something to connect to.

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
