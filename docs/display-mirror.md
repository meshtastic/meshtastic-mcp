# Display mirror: the device's own screen

`capture_display` and `drive_display_start` read the screen the firmware is rendering, streamed over the phone API, instead of photographing the panel with a webcam as `capture_screen` does. The pixels are exact, so OCR and comparisons are reliable. `display_mirror.py` holds the protocol.

The firmware needs screen-mirror support (meshtastic/firmware#11681). Older firmware ignores the admin verb silently, so the capture times out.

## `capture_display`

Captures one frame and writes a PNG. It handles both UIs: BaseUI's 1bpp framebuffer (`MONO_VLSB`) and MUI's (device-ui) RGB565 dirty rects.

**A blank frame.** `blank: true` almost always means the panel had already timed out (`display.screen_on_secs`, reported alongside it), not a mirroring failure. Capture again with `wake` set to an input event name or code, such as `"BACK"`. It is sent on the capture's own connection just before the capture arms, so the panel is lit when the frame is taken. A separate `send_input_event` first is not enough on a device whose connection handshake outlasts its screen timeout. The wake is a real key press, so choose one that is harmless on the screen being captured.

**Color.** MUI streams RGB565 directly. A color BaseUI device streams its 1bpp frame plus the region palette its panel paints with, which is applied here. `colorized` reports whether a palette was matched and applied; a monochrome panel has none and renders black and white. A device reporting `panel_class: EINK` renders black on white, as its panel paints.

**Files.** For documentation shots, pass `out_path` to name the file. Without it every capture overwrites one scratch path, so a walkthrough ends up as a single image. `out_path` must be a `.png` inside `MESHTASTIC_MCP_CAPTURE_DIR` (default: the server's working directory), and a relative path resolves there. Device screens carry mesh-sourced text that may steer the caller, so captures cannot land anywhere else.

**Scale.** `scale` enlarges the image by an integer factor (nearest neighbor, at most 8), which a 128x64 OLED needs to be legible on a page. `width` and `height` describe the written image, `panel_width` and `panel_height` the device.

Returns `{path, bytes, width, height, panel_width, panel_height, scale, format, blank, colorized, screen_on_secs, panel_class, frames, rects}`.

## `drive_display_start`

Runs input steps against the device UI and snapshots the mirrored screen after each. It holds one connection with mirroring armed, so a whole walkthrough costs one handshake. It returns a `job_id` at once; `job(job_id)` reports status and each finished step's PNG and `changed_px`.

`steps` is a list of at most 32 `{"event", "kb_char"?, "touch_x"?, "touch_y"?, "label"?}`. `event` takes the `send_input_event` names or codes:

| Input | Step |
| --- | --- |
| Arrow keys, select, back, cancel | `UP`, `DOWN`, `LEFT`, `RIGHT`, `SELECT`, `SELECT_LONG`, `BACK`, `CANCEL` |
| A tap | `USER_PRESS` with `touch_x`/`touch_y` |
| A touch long-press | `SELECT` with coordinates |
| Typing | any code with `kb_char` set, such as `{"event": 255, "kb_char": 104}` |

Each step reports `changed_px` against the previous snapshot, so a key the UI ignored shows 0. Frames stream only when the screen changes, so 0 can also mean the input landed on a no-op. `wake` presses one event before the first snapshot, as in `capture_display`.

These are real key presses:

- Enter (`SELECT` or `SELECT_LONG`) in a focused MUI text field sends the message on the mesh.
- `BACK` is the back action, except on a touch MUI board, which has no back key and ignores it; tap the sidebar instead.
- `CANCEL` turns a BaseUI screen off when no menu is open. Firmware without meshtastic/firmware#12358 needs it to close a menu.

PNGs go to `out_dir` inside `MESHTASTIC_MCP_CAPTURE_DIR`, or to a per-job directory under the data dir, and show mesh-sourced text.

## `send_input_event`

Injects one InputBroker event, the same code path as a physical button press. It takes a numeric code (0 to 255) or a name such as `"RIGHT"`, `"SELECT"` or `"FN_F1"`. Common codes: `SELECT`=10, `UP`=17, `DOWN`=18, `LEFT`=19, `RIGHT`=20, `CANCEL`=24, `BACK`=27, `FN_F1` to `FN_F5`=241 to 245. `kb_char`, `touch_x` and `touch_y` fill the event's other fields.
