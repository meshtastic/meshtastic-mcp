# Vision oracle (assert from pixels when the a11y tree fails)

The default app-plane oracle greps the accessibility tree (`android layout` /
`apple_sim.ui_dump`) for the marker token. That tree is **empty or unreliable** for:
- WebView / Flutter / Canvas / custom-drawn UI,
- mid-animation frames,
- map tiles, charts, and image-rendered text.

When the tree fails, fall back to a **vision oracle**: capture a screenshot and have the agent
(a vision-capable model) read it directly.

## How

1. Capture: `android screen capture -o shot.png` (or `apple_sim.screenshot(path)` /
   `xcrun simctl io <udid> screenshot`).
2. The agent inspects the image and answers the assertion as a yes/no with evidence, e.g.
   *"Does a message bubble containing `E2E-1782…` appear in the conversation?"* → quote the
   visible text + its location.
3. Treat it as the same boolean oracle as the tree grep — emit the same
   `LOOP … PASS|FAIL token=…` verdict.

## Rules

- **Prefer the a11y tree when it works** — it's deterministic, cheap, and exact-match. Use vision
  only as the documented fallback (the tree-empty case `harness.md` calls out).
- **Still use a unique marker token.** Vision reading a generic "hello" is as false-positive-prone
  as a grep; the token keeps it unambiguous.
- **Bounded polling applies.** Re-capture on an interval up to the deadline; mesh delivery is
  best-effort.
- **OCR is the non-agent fallback.** In a headless/CI path with no model in the loop, the optional
  OCR backend (`tesseract`/`easyocr`, the `[ui]` extra; see `doctor`) extracts text from the
  screenshot for a plain string match — lower fidelity than a VLM but scriptable.

## Why both

The a11y grep and the vision oracle catch different bugs: the tree can contain a token the user
can't see (off-screen, zero-alpha) and can miss a token the user clearly sees (custom drawing).
For a high-stakes assertion, agreement of both is the strongest signal.
