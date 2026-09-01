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
FROM_RADIO_DISPLAY_PALETTE = 21

# AdminMessage payload_variant field numbers (meshtastic/admin.proto).
ADMIN_GET_DISPLAY_FRAME_REQUEST = 50
ADMIN_SET_DISPLAY_MIRROR = 51

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
_F_PALETTE_SIGNATURE = 12

# DisplayPalette field numbers, and ColorRegion's within it.
_P_SIGNATURE = 1
_P_DEFAULT_ON = 2
_P_DEFAULT_OFF = 3
_P_REGION_OFFSET = 4
_P_REGION_TOTAL = 5
_P_REGIONS = 6
_R_X = 1
_R_Y = 2
_R_WIDTH = 3
_R_HEIGHT = 4
_R_ON_COLOR = 5
_R_OFF_COLOR = 6

# The firmware's region table is far smaller than this; the cap only bounds
# what a malformed or hostile stream can make us accumulate.
_MAX_PALETTE_REGIONS = 512

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


def admin_set_mirror(enabled: bool) -> bytes:
    """An `AdminMessage` carrying only `set_display_mirror`."""
    return encode_varint_field(ADMIN_SET_DISPLAY_MIRROR, 1 if enabled else 0)


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


def upscale(rgb: bytes, width: int, height: int, factor: int) -> tuple[bytes, int, int]:
    """Enlarge by an integer factor, nearest-neighbour.

    No interpolation on purpose: a device UI is a pixel grid, and smoothing it
    reads as a blurry photo of a screen rather than a screenshot.
    """
    if factor <= 1:
        return rgb, width, height
    row_bytes = width * 3
    out = bytearray(row_bytes * factor * height * factor)
    scaled_row_bytes = row_bytes * factor
    for y in range(height):
        row = bytearray(scaled_row_bytes)
        src = y * row_bytes
        for x in range(width):
            pixel = rgb[src + x * 3 : src + x * 3 + 3]
            base = x * factor * 3
            for k in range(factor):
                row[base + k * 3 : base + k * 3 + 3] = pixel
        for k in range(factor):
            dst = (y * factor + k) * scaled_row_bytes
            out[dst : dst + scaled_row_bytes] = row
    return bytes(out), width * factor, height * factor


def rgb565_to_rgb(value: int) -> bytes:
    """Expand one little-endian RGB565 pixel to an 8-bit RGB triple."""
    r5, g6, b5 = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
    return bytes(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))


# ── colour palette ─────────────────────────────────────────────────────────


class _Region:
    """One colorized rectangle, with its colours already expanded to RGB."""

    __slots__ = ("bottom", "left", "off", "on", "right", "top")

    def __init__(self, fields: dict[int, list[Any]]) -> None:
        self.left = int(field(fields, _R_X))
        self.top = int(field(fields, _R_Y))
        self.right = self.left + int(field(fields, _R_WIDTH))
        self.bottom = self.top + int(field(fields, _R_HEIGHT))
        self.on = rgb565_to_rgb(int(field(fields, _R_ON_COLOR)))
        self.off = rgb565_to_rgb(int(field(fields, _R_OFF_COLOR)))


class _Palette:
    """Accumulates `DisplayPalette` region chunks for one signature.

    A device that paints the 1bpp base UI onto a colour panel applies
    per-region on/off colours at flush time and streams the same table here,
    so the mirror renders in the panel's true colours at 1bpp bandwidth.
    Chunks are ordered by region index; anything out of sequence, or for a
    different signature, discards what was held.
    """

    def __init__(self) -> None:
        self.signature = 0
        self.default_on = b"\xff\xff\xff"
        self.default_off = b"\x00\x00\x00"
        self.regions: list[_Region] = []
        self._total = 0
        self._received = 0

    @property
    def complete(self) -> bool:
        return self._total > 0 and self._received >= self._total

    def handle(self, payload: bytes) -> None:
        try:
            fields = parse_fields(payload)
        except ValueError as exc:
            log.debug("display_palette: undecodable payload (%s)", exc)
            return

        signature = int(field(fields, _P_SIGNATURE))
        offset = int(field(fields, _P_REGION_OFFSET))
        total = int(field(fields, _P_REGION_TOTAL))
        chunks = [c for c in fields.get(_P_REGIONS, []) if isinstance(c, bytes)]

        if offset == 0:
            self.signature = signature
            self.regions = []
            self._received = 0
            self._total = total
            # Defaults are authoritative on the first chunk; later ones may omit them.
            self.default_on = rgb565_to_rgb(int(field(fields, _P_DEFAULT_ON)))
            self.default_off = rgb565_to_rgb(int(field(fields, _P_DEFAULT_OFF)))
        elif signature != self.signature or offset != self._received:
            log.debug("display_palette: dropping chunk %d@%d", signature, offset)
            self.regions = []
            self._received = 0
            self._total = 0
            return

        if total > _MAX_PALETTE_REGIONS or self._received + len(chunks) > _MAX_PALETTE_REGIONS:
            log.debug("display_palette: region table over cap (%d)", total)
            self.regions = []
            self._received = 0
            self._total = 0
            return

        for chunk in chunks:
            try:
                self.regions.append(_Region(parse_fields(chunk)))
            except ValueError as exc:
                log.debug("display_palette: undecodable region (%s)", exc)
                return
        self._received += len(chunks)

    def colors_at(self, x: int, row_regions: list[_Region]) -> tuple[bytes, bytes]:
        """(on, off) for a pixel, highest table index winning where regions overlap."""
        for region in reversed(row_regions):
            if region.left <= x < region.right:
                return region.on, region.off
        return self.default_on, self.default_off

    def regions_on_row(self, y: int) -> list[_Region]:
        return [r for r in self.regions if r.top <= y < r.bottom]


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
        self.palette = _Palette()
        self._mono: bytearray | None = None
        self._mono_palette_sig = 0
        self._buf: bytearray | None = None
        self._received = 0
        self._frame_id = 0
        self._rect: tuple[int, int, int, int] | None = None

    @property
    def complete(self) -> bool:
        """True once at least one whole frame (or rect) has arrived."""
        return self._mono is not None or (self.rgb is not None and self.rects > 0)

    def render(self) -> bytes | None:
        """The finished RGB canvas.

        Deferred to the end of a capture rather than done per chunk: the
        palette may arrive after the frame it colours (the proto allows
        display_palette to interleave), and rendering early would freeze the
        frame as monochrome.
        """
        if self._mono is not None:
            return self._render_mono(self._mono)
        return bytes(self.rgb) if self.rgb is not None else None

    @property
    def colorized(self) -> bool:
        """True when a palette matching the captured frame was applied."""
        return (
            self._mono is not None
            and self._mono_palette_sig != 0
            and self.palette.signature == self._mono_palette_sig
            # A half-received table would colour some regions and not others.
            and self.palette.complete
        )

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

        self._mono = buf
        self._mono_palette_sig = int(field(fields, _F_PALETTE_SIGNATURE))
        self._buf = None
        self.frames += 1
        self.format = FORMAT_MONO_VLSB

    def _render_mono(self, buf: bytearray) -> bytes:
        """Expand the 1bpp frame, colorizing through the palette when it matches.

        A frame naming a palette signature we do not hold renders monochrome,
        which is what the proto asks for.
        """
        palette = self.palette if self.colorized else None
        rgb = bytearray(self.width * self.height * 3)
        plain_on, plain_off = b"\xff\xff\xff", b"\x00\x00\x00"
        for y in range(self.height):
            page = (y // _PIXELS_PER_PAGE) * self.width
            bit = 1 << (y % _PIXELS_PER_PAGE)
            row = y * self.width
            row_regions = palette.regions_on_row(y) if palette else []
            for x in range(self.width):
                index = page + x
                lit = index < len(buf) and buf[index] & bit
                if palette is None:
                    on, off = plain_on, plain_off
                else:
                    on, off = palette.colors_at(x, row_regions)
                dst = (row + x) * 3
                rgb[dst : dst + 3] = on if lit else off
        return bytes(rgb)

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

# Re-request if nothing has arrived by then.
_RETRY_S = 4.0

# Let the config handshake finish before speaking.
_SETTLE_S = 2.0

# Device panels are small (128x64 up to 320x240), so a documentation
# screenshot usually wants enlarging. Nearest-neighbour keeps the pixel grid
# crisp, which is what reads well for a device UI; 8x of the largest panel is
# 2560x1920, past any reasonable page width.
MAX_SCALE = 8


def capture(port: str | None = None, timeout_s: float = 20.0, scale: int = 1) -> dict[str, Any]:
    """Request one frame of the device's screen and return it as PNG bytes.

    Arms mirroring, requests a frame, and disarms in a `finally`.

    The one-shot `get_display_frame_request` on its own looks tidier — the
    firmware clears it once the frame has drained, so it cannot leave the
    device streaming — but on the MUI (LVGL) path it does not reliably
    produce a frame, verified on a T-Deck: arm-plus-request returns a rect
    every time where request-alone times out. Disarming on the way out stops
    the device streaming to nobody, and `PhoneAPI::close()` disarms again
    when the connection drops, so an abandoned call cannot leave it armed.

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
                fields = parse_fields(payload)
                # Palette first: the firmware sends it ahead of the frame so a
                # client can colorize the first frame it renders.
                for raw in fields.get(FROM_RADIO_DISPLAY_PALETTE, []):
                    if isinstance(raw, bytes):
                        canvas.palette.handle(raw)
                        last_chunk = time.monotonic()
                for raw in fields.get(FROM_RADIO_DISPLAY_FRAME, []):
                    if isinstance(raw, bytes):
                        canvas.handle(raw)
                        last_chunk = time.monotonic()
            except ValueError as exc:
                log.debug("display_frame: unparsable FromRadio (%s)", exc)
            return original(payload, **kwargs)

        iface._handleFromRadio = types.MethodType(patched, iface)
        try:
            # connect() yields the moment the interface is constructed. An
            # admin message sent into the tail of the config handshake is
            # silently dropped, so settle first and re-send the whole pair
            # (not just the request — arming is the half that matters) if
            # nothing has arrived.
            time.sleep(_SETTLE_S)
            _arm_and_request(iface)
            deadline = time.monotonic() + budget
            retry_at = time.monotonic() + _RETRY_S
            while time.monotonic() < deadline:
                if canvas.complete and time.monotonic() - last_chunk >= _QUIET_S:
                    break
                if not canvas.complete and time.monotonic() >= retry_at:
                    _arm_and_request(iface)
                    retry_at = time.monotonic() + _RETRY_S
                time.sleep(_POLL_S)
        finally:
            try:
                _send_admin(iface, admin_set_mirror(False))
            except Exception as exc:  # never mask the real result
                log.debug("display mirror: disarm failed (%s)", exc)
            # Instance attribute, so this never leaks into other interfaces
            # held by the recorder/replay/cot_relay sessions in this process.
            iface._handleFromRadio = original

        screen_on_secs = _screen_on_secs(iface)

    rgb = canvas.render()
    if not canvas.complete or rgb is None:
        raise TimeoutError(
            f"no display frame within {budget:g}s — the device must run firmware with "
            "screen-mirror support (meshtastic/firmware#11681); older firmware ignores "
            "the get_display_frame_request admin verb silently"
        )

    factor = max(1, min(int(scale), MAX_SCALE))
    scaled, width, height = upscale(rgb, canvas.width, canvas.height, factor)
    return {
        "png": write_png(width, height, scaled),
        "width": width,
        "height": height,
        "panel_width": canvas.width,
        "panel_height": canvas.height,
        "scale": factor,
        "format": "RGB565" if canvas.format == FORMAT_RGB565 else "MONO_VLSB",
        "frames": canvas.frames,
        "rects": canvas.rects,
        "colorized": canvas.colorized,
        "blank": _looks_blank(rgb),
        "screen_on_secs": screen_on_secs,
    }


def _screen_on_secs(iface: Any) -> int | None:
    """The panel's blank timeout, reported alongside a blank capture."""
    try:
        return int(iface.localNode.localConfig.display.screen_on_secs)
    except Exception:  # pragma: no cover - optional hint, never fatal
        return None


def _arm_and_request(iface: Any) -> None:
    """Arm mirroring and ask for a frame."""
    _send_admin(iface, admin_set_mirror(True))
    _send_admin(iface, admin_request_frame())


def _send_admin(iface: Any, payload: bytes) -> None:
    """Send a pre-encoded AdminMessage as raw bytes on the admin portnum.

    Deliberately not `localNode._sendAdmin`: that expects a generated
    `AdminMessage` (it assigns `session_passkey` onto it), and these fields
    are new in protobufs#1054. `sendData` takes bytes as-is, and a local admin
    message needs no session key.
    """
    from meshtastic import portnums_pb2  # type: ignore[import-untyped]

    iface.sendData(
        payload,
        destinationId=iface.myInfo.my_node_num,
        portNum=portnums_pb2.PortNum.ADMIN_APP,
        wantAck=False,
        wantResponse=False,
    )
