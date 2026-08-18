# Plan: expose deterministic Android app-plane driving as MCP tools

Goal: let an agent drive a running Meshtastic-Android build (emulator or
physical, live in a session) the same deterministic, view-hierarchy-based way
`scripts/ci_android_app_loop.py` already does — without writing a throwaway
Python script each time.

## Current state

`src/meshtastic_mcp/emulator/avd.py` already has a mature app-plane driving
layer, built and validated for the CI soak loops (`ci_android_app_loop.py`,
`ci_atak_app_loop.py`) and unit-tested (`tests/unit/test_emulator_avd.py`):

- `ui_dump()` — parsed view-hierarchy (`android layout` on emulator,
  `adb exec-out uiautomator dump` on physical), schema-compatible across both
- `tap()` / `swipe()` / `type_text()` — raw-coordinate input via `adb shell input`
- `find_text()` / `poll_for_text()` — the bounded text-oracle; this is the
  actual anti-flake primitive (wait for state, not `sleep`)
- `screenshot(annotate=True)` — on emulator, wraps the Android CLI's
  `android screen capture --annotate` (labeled bounding boxes, pairs with
  `android screen resolve`); on physical, silently ignores `annotate`
- `clear_logcat()` / `read_logcat()` — log oracle

None of it is registered as an MCP tool. Only `avd.docs_search` /
`avd.docs_fetch` / `avd.version_lookup` / `avd.render_compose_preview` are
exposed (`server.py`). The driving primitives are reachable only from Python
scripts and tests — not from a live agent session.

The launch entrypoint (`adb shell am start ... --ez skip_onboarding true` +
deep links, documented in `android/.skills/testing-ci/SKILL.md`) is
out of scope here and stays the documented entrypoint. These tools are for
post-launch in-app navigation and verification only.

## Design

### 1. Extend the uiautomator/layout schema to keep full bounds + a stable label

`_parse_uiautomator_xml` (avd.py:524) currently reduces each element's
`bounds` rect to a `center` point and discards the rect. Extend the per-element
dict to also carry:

- `bounds`: `[x1, y1, x2, y2]`
- `label`: a stable per-dump integer, assigned in walk order — matches the
  `#<number>` convention `android screen resolve` already uses for the
  emulator path, so the two code paths present the same interaction model.

`android layout`'s JSON output (emulator path) already returns full bounds:
confirm the field name and normalize it into the same `bounds`/`label` shape
so downstream code doesn't need to know which backend produced a given dump.

### 2. Physical-device annotated screenshots

Physical devices don't get `android screen capture --annotate` today — a
prior investigation (see `git log -- emulator/avd.py`, the deep-link nav fix)
deliberately kept physical-device paths on `adb`-only. Rather than relying on
an undocumented `--device` flag against real hardware, render the boxes
ourselves: capture via `adb exec-out screencap -p`, dump the hierarchy via (1),
draw labeled boxes with Pillow (already a dependency in the `[ui]` extra — no
new dependency). Feature-detect Pillow the way `camera.py`/`ocr.py` degrade
when optional deps are missing; annotate on physical devices needs `[ui]`
installed, plain screenshots don't.

### 3. `avd.resolve_label(label, serial=None) -> tuple[int, int]`

Emulator: delegates to `android screen resolve --screenshot=<path>
--string="#<label>"` and parses the coordinates back out. Physical: looks up
`label` in the last dump produced by (1)/(2) for that serial (small in-memory
cache, most-recent-dump-per-serial).

### 4. New MCP tools (gated under the existing `android` capability)

| Tool | Wraps |
|---|---|
| `android_ui_dump(serial?, diff?)` | `avd.ui_dump` |
| `android_screenshot(serial?, annotate?)` | `avd.screenshot`, unified emulator/physical |
| `android_resolve(serial?, label)` | new `avd.resolve_label` |
| `android_tap(serial?, x?, y?, label?)` | `avd.tap`, coords or label |
| `android_swipe(serial?, x1, y1, x2, y2, ms?)` | `avd.swipe` |
| `android_type_text(serial?, text)` | `avd.type_text` |
| `android_find_text(serial?, token)` | `avd.find_text` |
| `android_poll_for_text(serial?, token, timeout?, interval?)` | `avd.poll_for_text` |
| `android_clear_logcat(serial?)` | `avd.clear_logcat` |
| `android_read_logcat(serial?, tags?, grep?)` | `avd.read_logcat` |

## Testing

- Extend `tests/unit/test_emulator_avd.py` for: bounds retained in
  `_parse_uiautomator_xml`, label assignment stability, `resolve_label` on
  both backends, Pillow-based annotation rendering (physical path).
- Register new tools in `tests/tool_coverage.py` (existing pattern — every
  registered MCP tool needs a coverage entry).
- Live validation: emulator smoke pass (`ci_android_app_loop.py`-style
  connect + navigate) plus one physical-device pass, mirroring how the
  deep-link nav fix was verified live against a Pixel 6a.
