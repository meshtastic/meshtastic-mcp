# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture the device's own screen over the phone API.

The firmware streams its framebuffer to a locally attached client as
`FromRadio.display_frame` chunks, armed by the `get_display_frame_request`
admin verb. This is the on-device counterpart to `camera.py`: same question
("what is on the screen?"), but read from the framebuffer instead of
photographed, so the pixels are exact and OCR is reliable.

Two wire formats, both handled here:

* **MONO_VLSB** — BaseUI's 1bpp framebuffer, one byte per 8 vertically
  adjacent pixels. Sent as one whole frame, chunked by offset.
* **RGB565** — MUI (device-ui/LVGL) dirty rects, native little-endian, each
  rect chunked by offset and composited onto a persistent canvas.

Everything on the wire is device-controlled, so every field is validated
before anything is allocated — see `_MirrorCanvas`.

Both the admin verb and the `display_frame` payload are hand-encoded and
hand-parsed: they are new in protobufs#1054 and the released `meshtastic`
package's generated bindings do not carry them yet. When that ships, these
helpers can be replaced by the generated messages with no change to the
tool surface.
"""

from __future__ import annotations

import logging
import struct
import time
import types
import zlib
from typing import Any

log = logging.getLogger("meshtastic_mcp.display_mirror")

# FromRadio payload_variant field numbers (meshtastic/mesh.proto).
FROM_RADIO_DISPLAY_FRAME = 20

# AdminMessage payload_variant field numbers (meshtastic/admin.proto).
ADMIN_GET_DISPLAY_FRAME_REQUEST = 50

# DisplayFrame.Format
FORMAT_MONO_VLSB = 1
FORMAT_RGB565 = 2

# DisplayFrame field numbers.
_F_WIDTH = 1
_F_HEIGHT = 2
_F_FORMAT = 3
_F_FRAME_ID = 4
_F_OFFSET = 5
_F_TOTAL_SIZE = 6
_F_DATA = 7
_F_RECT_X = 8
_F_RECT_Y = 9
_F_RECT_WIDTH = 10
_F_RECT_HEIGHT = 11

# MONO_VLSB packs 8 vertically adjacent pixels per byte (one "page" row).
_PIXELS_PER_PAGE = 8

# Sanity bound on a panel edge; the largest real Meshtastic panel is 800x480.
_MAX_PANEL_EDGE = 1024

# A mono frame is 1bpp, so this is far above any real panel (320x240 = 9600 B).
_MAX_MONO_BYTES = 16384

# A single rect can at most cover the largest panel we accept.
_MAX_RECT_BYTES = _MAX_PANEL_EDGE * _MAX_PANEL_EDGE * 2

# Upper bound on `capture_display(timeout_s=...)`. An MCP call that can outrun
# 60s is supposed to be a job (AGENTS.md), and a capture is a couple of seconds
# of streaming; this only caps the damage from a bad argument.
MAX_TIMEOUT_S = 45.0


# ── minimal protobuf codec ─────────────────────────────────────────────────


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def parse_fields(buf: bytes) -> dict[int, list[Any]]:
    """Decode a protobuf message into `{field_number: [values]}`.

    Varints decode to `int`, length-delimited to `bytes`, and the fixed
    widths to `bytes`. Enough to read a `DisplayFrame` without generated
    bindings; unknown fields are kept rather than rejected.
    """
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        number, wire = key >> 3, key & 7
        value: Any
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 2:
            length, i = _read_varint(buf, i)
            if i + length > len(buf):
                raise ValueError("truncated length-delimited field")
            value, i = buf[i : i + length], i + length
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(number, []).append(value)
    return out


def field(fields: dict[int, list[Any]], number: int, default: Any = 0) -> Any:
    """First value of a field, or `default` when absent."""
    values = fields.get(number)
    return values[0] if values else default


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def encode_varint_field(number: int, value: int) -> bytes:
    """Encode one varint field (wire type 0)."""
    return _encode_varint(number << 3) + _encode_varint(value)


def admin_request_frame() -> bytes:
    """An `AdminMessage` carrying only `get_display_frame_request = true`."""
    return encode_varint_field(ADMIN_GET_DISPLAY_FRAME_REQUEST, 1)


# ── PNG output ─────────────────────────────────────────────────────────────


def write_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB triples as a PNG. Pure stdlib — no Pillow needed."""
    raw = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def rgb565_to_rgb(value: int) -> bytes:
    """Expand one little-endian RGB565 pixel to an 8-bit RGB triple."""
    r5, g6, b5 = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
    return bytes(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))


# ── frame reassembly ───────────────────────────────────────────────────────


class _MirrorCanvas:
    """Reassembles `DisplayFrame` chunks into an RGB canvas.

    A frame arrives as contiguous, offset-ordered chunks (FromRadio is a
    reliable ordered stream), so a chunk at `offset == 0` starts a frame and
    anything out of sequence drops the partial one. Rect geometry is latched
    at `offset == 0` so a later chunk cannot transpose an in-flight blit.
    """

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.rgb: bytearray | None = None
        self.frames = 0
        self.rects = 0
        self.format: int | None = None
        self._buf: bytearray | None = None
        self._received = 0
        self._frame_id = 0
        self._rect: tuple[int, int, int, int] | None = None

    @property
    def complete(self) -> bool:
        """True once at least one whole frame (or rect) has been composited."""
        return self.rgb is not None and (self.frames or self.rects) > 0

    def handle(self, payload: bytes) -> None:
        """Feed one `DisplayFrame` message. Malformed input is dropped, not raised."""
        try:
            fields = parse_fields(payload)
        except ValueError as exc:
            log.debug("display_frame: undecodable payload (%s)", exc)
            return
        fmt = int(field(fields, _F_FORMAT) or FORMAT_MONO_VLSB)
        if fmt == FORMAT_RGB565:
            self._handle_rect(fields)
        else:
            self._handle_mono(fields)

    # -- shared -----------------------------------------------------------

    def _ensure_canvas(self, width: int, height: int) -> bytearray:
        if self.rgb is None or (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.rgb = bytearray(width * height * 3)
        return self.rgb

    def _in_sequence(self, fields: dict[int, list[Any]]) -> bool:
        """A continuation must match the buffer actually allocated at offset 0,
        not merely its own `total_size` claim."""
        offset = int(field(fields, _F_OFFSET))
        if offset == 0:
            return True
        return (
            self._buf is not None
            and len(self._buf) == int(field(fields, _F_TOTAL_SIZE))
            and int(field(fields, _F_FRAME_ID)) == self._frame_id
            and offset == self._received
        )

    # -- MONO_VLSB --------------------------------------------------------

    def _handle_mono(self, fields: dict[int, list[Any]]) -> None:
        width = int(field(fields, _F_WIDTH))
        height = int(field(fields, _F_HEIGHT))
        total = int(field(fields, _F_TOTAL_SIZE))
        offset = int(field(fields, _F_OFFSET))
        data: bytes = field(fields, _F_DATA, b"")

        pages = (height + _PIXELS_PER_PAGE - 1) // _PIXELS_PER_PAGE
        valid = (
            0 < width <= _MAX_PANEL_EDGE
            and 0 < height <= _MAX_PANEL_EDGE
            and total == width * pages
            and total <= _MAX_MONO_BYTES
            and offset + len(data) <= total
        )
        if not valid or not self._in_sequence(fields):
            log.debug("display_frame: dropping mono chunk %dx%d total=%d", width, height, total)
            self._buf = None
            return

        if offset == 0:
            self._buf = bytearray(total)
            self._frame_id = int(field(fields, _F_FRAME_ID))
            self._received = 0
            self.width, self.height = width, height

        buf = self._buf
        if buf is None:
            return
        buf[offset : offset + len(data)] = data
        self._received += len(data)
        if self._received < len(buf):
            return

        self._render_mono(buf)
        self._buf = None
        self.frames += 1
        self.format = FORMAT_MONO_VLSB

    def _render_mono(self, buf: bytearray) -> None:
        rgb = self._ensure_canvas(self.width, self.height)
        on, off = b"\xff\xff\xff", b"\x00\x00\x00"
        for y in range(self.height):
            page = (y // _PIXELS_PER_PAGE) * self.width
            bit = 1 << (y % _PIXELS_PER_PAGE)
            row = y * self.width
            for x in range(self.width):
                index = page + x
                lit = index < len(buf) and buf[index] & bit
                dst = (row + x) * 3
                rgb[dst : dst + 3] = on if lit else off

    # -- RGB565 dirty rects -----------------------------------------------

    def _handle_rect(self, fields: dict[int, list[Any]]) -> None:
        width = int(field(fields, _F_WIDTH))
        height = int(field(fields, _F_HEIGHT))
        total = int(field(fields, _F_TOTAL_SIZE))
        offset = int(field(fields, _F_OFFSET))
        data: bytes = field(fields, _F_DATA, b"")
        # A rect that omits width or height covers the full panel in that axis.
        rect_w = int(field(fields, _F_RECT_WIDTH)) or width
        rect_h = int(field(fields, _F_RECT_HEIGHT)) or height
        rect_x = int(field(fields, _F_RECT_X))
        rect_y = int(field(fields, _F_RECT_Y))

        valid = (
            self._rect_within_panel(width, height, rect_x, rect_y, rect_w, rect_h)
            and total == rect_w * rect_h * 2
            and 0 < total <= _MAX_RECT_BYTES
            and offset + len(data) <= total
        )
        if not valid or not self._in_sequence(fields):
            log.debug("display_frame: dropping rect %dx%d total=%d", rect_w, rect_h, total)
            self._buf = None
            return

        if offset == 0:
            self._buf = bytearray(total)
            self._frame_id = int(field(fields, _F_FRAME_ID))
            self._received = 0
            self._rect = (rect_x, rect_y, rect_w, rect_h)
            self._ensure_canvas(width, height)

        buf = self._buf
        if buf is None:
            return
        buf[offset : offset + len(data)] = data
        self._received += len(data)
        if self._received < len(buf):
            return

        self._blit_rect(buf)
        self._buf = None
        self.rects += 1
        self.format = FORMAT_RGB565

    @staticmethod
    def _rect_within_panel(
        width: int, height: int, rect_x: int, rect_y: int, rect_w: int, rect_h: int
    ) -> bool:
        """Subtraction form throughout: `rect_x + rect_w` overflows for hostile values."""
        return (
            0 < width <= _MAX_PANEL_EDGE
            and 0 < height <= _MAX_PANEL_EDGE
            and 0 < rect_w <= width
            and 0 < rect_h <= height
            and 0 <= rect_x <= width - rect_w
            and 0 <= rect_y <= height - rect_h
        )

    def _blit_rect(self, buf: bytearray) -> None:
        rgb = self.rgb
        rect = self._rect
        if rgb is None or rect is None:
            return
        x0, y0, rect_w, rect_h = rect
        for row in range(rect_h):
            src = row * rect_w * 2
            dst_row = (y0 + row) * self.width
            for col in range(rect_w):
                pixel = buf[src + col * 2] | (buf[src + col * 2 + 1] << 8)
                dst = (dst_row + x0 + col) * 3
                rgb[dst : dst + 3] = rgb565_to_rgb(pixel)


def _looks_blank(rgb: bytes) -> bool:
    """True when every pixel is identical.

    Tested as "all one colour", not "all black": an inverted or e-ink panel
    blanks to white. A blank capture almost always means the panel had
    already timed out (`display.screen_on_secs`), not that mirroring failed.
    """
    return len(rgb) >= 3 and rgb == rgb[0:3] * (len(rgb) // 3)


# ── capture ────────────────────────────────────────────────────────────────

# After the first complete frame/rect, keep collecting until the stream has
# been quiet this long. A full repaint reaches us as a burst — LVGL may split
# one screen into several rects — so stopping at the first completion can
# capture a half-drawn screen.
_QUIET_S = 1.0

_POLL_S = 0.05


def capture(port: str | None = None, timeout_s: float = 20.0) -> dict[str, Any]:
    """Request one frame of the device's screen and return it as PNG bytes.

    Uses the one-shot `get_display_frame_request` verb rather than arming
    continuous mirroring: the firmware clears the one-shot only once the whole
    frame has drained, so it is self-limiting and cannot leave the device
    streaming (or holding the MUI rect pool) if this call dies partway.

    Raises `TimeoutError` when no frame arrives, which on this wire is
    indistinguishable from firmware that has never heard of the verb — the
    message says so.
    """
    from .connection import connect

    budget = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))
    canvas = _MirrorCanvas()
    last_chunk = 0.0

    with connect(port=port) as iface:
        original = iface._handleFromRadio

        def patched(self: Any, payload: bytes, **kwargs: Any) -> Any:
            nonlocal last_chunk
            try:
                for raw in parse_fields(payload).get(FROM_RADIO_DISPLAY_FRAME, []):
                    if isinstance(raw, bytes):
                        canvas.handle(raw)
                        last_chunk = time.monotonic()
            except ValueError as exc:
                log.debug("display_frame: unparsable FromRadio (%s)", exc)
            return original(payload, **kwargs)

        iface._handleFromRadio = types.MethodType(patched, iface)
        try:
            _request_frame(iface)
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                if canvas.complete and time.monotonic() - last_chunk >= _QUIET_S:
                    break
                time.sleep(_POLL_S)
        finally:
            # Instance attribute, so this never leaks into other interfaces
            # held by the recorder/replay/cot_relay sessions in this process.
            iface._handleFromRadio = original

        screen_on_secs = _screen_on_secs(iface)

    if not canvas.complete or canvas.rgb is None:
        raise TimeoutError(
            f"no display frame within {budget:g}s — the device must run firmware with "
            "screen-mirror support (meshtastic/firmware#11681); older firmware ignores "
            "the get_display_frame_request admin verb silently"
        )

    rgb = bytes(canvas.rgb)
    return {
        "png": write_png(canvas.width, canvas.height, rgb),
        "width": canvas.width,
        "height": canvas.height,
        "format": "RGB565" if canvas.format == FORMAT_RGB565 else "MONO_VLSB",
        "frames": canvas.frames,
        "rects": canvas.rects,
        "blank": _looks_blank(rgb),
        "screen_on_secs": screen_on_secs,
    }


def _screen_on_secs(iface: Any) -> int | None:
    """The panel's blank timeout, reported alongside a blank capture."""
    try:
        return int(iface.localNode.localConfig.display.screen_on_secs)
    except Exception:  # pragma: no cover - optional hint, never fatal
        return None


def _request_frame(iface: Any) -> None:
    """Send the one-shot request as raw bytes on the admin portnum.

    Deliberately not `localNode._sendAdmin`: that expects a generated
    `AdminMessage` (it assigns `session_passkey` onto it), and the field is
    new in protobufs#1054. `sendData` takes bytes as-is, and a local admin
    message needs no session key.
    """
    from meshtastic import portnums_pb2  # type: ignore[import-untyped]

    iface.sendData(
        admin_request_frame(),
        destinationId=iface.myInfo.my_node_num,
        portNum=portnums_pb2.PortNum.ADMIN_APP,
        wantAck=False,
        wantResponse=False,
    )
