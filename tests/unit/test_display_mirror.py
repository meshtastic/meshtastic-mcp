# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Frame reassembly, validation and PNG encoding for `display_mirror`.

Everything here is pure — no device, no serial port. The hostile-input cases
are the point: `total_size` and the rect geometry are device-controlled, so
each one must be rejected before anything is allocated.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from meshtastic_mcp import display_mirror as dm

# ── wire helpers ───────────────────────────────────────────────────────────


def _bytes_field(number: int, payload: bytes) -> bytes:
    """Wire type 2: tag, then length, then the bytes."""
    return dm._encode_varint((number << 3) | 2) + dm._encode_varint(len(payload)) + payload


def _frame(
    *,
    width: int,
    height: int,
    fmt: int,
    total: int,
    data: bytes,
    offset: int = 0,
    frame_id: int = 1,
    rect: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Encode a DisplayFrame the way the firmware does."""
    out = dm.encode_varint_field(dm._F_WIDTH, width)
    out += dm.encode_varint_field(dm._F_HEIGHT, height)
    out += dm.encode_varint_field(dm._F_FORMAT, fmt)
    out += dm.encode_varint_field(dm._F_FRAME_ID, frame_id)
    out += dm.encode_varint_field(dm._F_OFFSET, offset)
    out += dm.encode_varint_field(dm._F_TOTAL_SIZE, total)
    out += _bytes_field(dm._F_DATA, data)
    if rect is not None:
        x, y, w, h = rect
        out += dm.encode_varint_field(dm._F_RECT_X, x)
        out += dm.encode_varint_field(dm._F_RECT_Y, y)
        out += dm.encode_varint_field(dm._F_RECT_WIDTH, w)
        out += dm.encode_varint_field(dm._F_RECT_HEIGHT, h)
    return out


def _rect_frame(
    width: int, height: int, rect: tuple[int, int, int, int], pixels: bytes, **kw
) -> bytes:
    _, _, w, h = rect
    return _frame(
        width=width,
        height=height,
        fmt=dm.FORMAT_RGB565,
        total=kw.pop("total", w * h * 2),
        data=kw.pop("data", pixels),
        rect=rect,
        **kw,
    )


def _le565(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


RED = 0xF800
GREEN = 0x07E0


# ── protobuf codec ─────────────────────────────────────────────────────────


def test_varint_round_trip_across_byte_boundaries() -> None:
    for value in (0, 1, 127, 128, 300, 16383, 16384, 2**31):
        encoded = dm.encode_varint_field(5, value)
        assert dm.field(dm.parse_fields(encoded), 5) == value


def test_parse_fields_keeps_repeated_and_unknown_fields() -> None:
    buf = (
        dm.encode_varint_field(1, 7) + dm.encode_varint_field(1, 9) + dm.encode_varint_field(99, 3)
    )
    fields = dm.parse_fields(buf)
    assert fields[1] == [7, 9]
    assert dm.field(fields, 99) == 3
    assert dm.field(fields, 42, "fallback") == "fallback"


@pytest.mark.parametrize(
    "buf",
    [
        b"\xff",  # varint that never terminates
        b"\x0a\x05ab",  # length-delimited claiming more than it carries
        b"\x1f",  # wire type 7 is not a thing
    ],
)
def test_parse_fields_rejects_malformed_input(buf: bytes) -> None:
    with pytest.raises(ValueError):
        dm.parse_fields(buf)


def test_admin_request_frame_sets_the_one_shot_verb() -> None:
    fields = dm.parse_fields(dm.admin_request_frame())
    assert dm.field(fields, dm.ADMIN_GET_DISPLAY_FRAME_REQUEST) == 1


# ── PNG ────────────────────────────────────────────────────────────────────


def test_write_png_is_decodable_and_carries_the_pixels() -> None:
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    png = dm.write_png(2, 2, rgb)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert (width, height, depth, colour) == (2, 2, 8, 2)

    idat = png[png.index(b"IDAT") + 4 : png.rindex(b"IEND") - 8]
    raw = zlib.decompress(idat)
    # One filter byte per row, then the row's RGB triples.
    assert raw == b"\x00" + rgb[:6] + b"\x00" + rgb[6:]


def test_rgb565_expands_to_full_range_endpoints() -> None:
    assert dm.rgb565_to_rgb(0x0000) == b"\x00\x00\x00"
    assert dm.rgb565_to_rgb(0xFFFF) == b"\xff\xff\xff"
    assert dm.rgb565_to_rgb(RED) == b"\xff\x00\x00"


# ── MONO_VLSB ──────────────────────────────────────────────────────────────


def test_mono_frame_lights_the_expected_pixels() -> None:
    # 8x8: one page, bit 0 of every byte = the top row lit.
    canvas = dm._MirrorCanvas()
    canvas.handle(_frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=8, data=b"\x01" * 8))

    assert canvas.complete
    assert (canvas.width, canvas.height, canvas.frames) == (8, 8, 1)
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[:24] == b"\xff\xff\xff" * 8  # row 0 lit
    assert rgb[24:48] == b"\x00\x00\x00" * 8  # row 1 dark


def test_mono_height_not_a_multiple_of_eight_still_rounds_up_to_whole_pages() -> None:
    # 8x12 needs 2 pages (16 bytes), not 12 — the validator and the renderer
    # must agree, or a legitimate frame is rejected.
    canvas = dm._MirrorCanvas()
    canvas.handle(_frame(width=8, height=12, fmt=dm.FORMAT_MONO_VLSB, total=16, data=b"\xff" * 16))
    assert canvas.complete
    assert (canvas.width, canvas.height) == (8, 12)


def test_mono_reassembles_across_chunks() -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(_frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=8, data=b"\x01" * 4))
    assert not canvas.complete  # half a frame is not a frame
    canvas.handle(
        _frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=8, data=b"\x01" * 4, offset=4)
    )
    assert canvas.complete


def test_mono_size_must_match_the_declared_geometry() -> None:
    canvas = dm._MirrorCanvas()
    # 8x8 is 8 bytes; a frame claiming 9 is lying about one of the three.
    canvas.handle(_frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=9, data=b"\x01" * 9))
    assert not canvas.complete


def test_mono_rejects_zero_dimensions() -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(_frame(width=0, height=8, fmt=dm.FORMAT_MONO_VLSB, total=0, data=b""))
    assert not canvas.complete


# ── RGB565 dirty rects ─────────────────────────────────────────────────────


def test_rect_composites_at_its_offset_and_leaves_the_rest_untouched() -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(_rect_frame(4, 4, (1, 1, 2, 2), _le565(RED) * 4))

    assert canvas.complete
    assert (canvas.width, canvas.height, canvas.rects) == (4, 4, 1)
    assert canvas.rgb is not None
    rgb = canvas.rgb

    def px(x: int, y: int) -> bytes:
        start = (y * 4 + x) * 3
        return bytes(rgb[start : start + 3])

    assert px(1, 1) == b"\xff\x00\x00"
    assert px(2, 2) == b"\xff\x00\x00"
    assert px(0, 0) == b"\x00\x00\x00"  # outside the rect
    assert px(3, 3) == b"\x00\x00\x00"


def test_successive_rects_accumulate_on_one_canvas() -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(_rect_frame(4, 4, (0, 0, 1, 1), _le565(RED)))
    canvas.handle(_rect_frame(4, 4, (3, 3, 1, 1), _le565(GREEN), frame_id=2))

    assert canvas.rects == 2
    assert canvas.rgb is not None
    assert bytes(canvas.rgb[0:3]) == b"\xff\x00\x00"  # the first rect survived
    assert bytes(canvas.rgb[45:48]) == b"\x00\xff\x00"


def test_rect_omitting_width_and_height_covers_the_whole_panel() -> None:
    # rect_width/rect_height == 0 means "all of this axis", not "empty".
    canvas = dm._MirrorCanvas()
    canvas.handle(
        _frame(
            width=2,
            height=2,
            fmt=dm.FORMAT_RGB565,
            total=8,
            data=_le565(GREEN) * 4,
            rect=(0, 0, 0, 0),
        )
    )
    assert canvas.complete
    assert canvas.rgb is not None
    assert bytes(canvas.rgb) == b"\x00\xff\x00" * 4


def test_rect_geometry_is_latched_at_offset_zero() -> None:
    """A continuation must not be able to transpose an in-flight blit."""
    canvas = dm._MirrorCanvas()
    pixels = _le565(RED) * 4
    canvas.handle(_rect_frame(4, 4, (0, 0, 2, 2), pixels[:4]))
    # Same frame_id and the expected offset, but claiming a different origin.
    canvas.handle(_rect_frame(4, 4, (2, 2, 2, 2), pixels[4:], offset=4))

    assert canvas.rects == 1
    assert canvas.rgb is not None
    # Blitted at the latched (0,0), so the far corner stayed black.
    assert bytes(canvas.rgb[0:3]) == b"\xff\x00\x00"
    assert bytes(canvas.rgb[45:48]) == b"\x00\x00\x00"


def test_out_of_sequence_chunk_drops_the_partial_rect() -> None:
    canvas = dm._MirrorCanvas()
    pixels = _le565(RED) * 4
    canvas.handle(_rect_frame(4, 4, (0, 0, 2, 2), pixels[:4]))
    canvas.handle(_rect_frame(4, 4, (0, 0, 2, 2), pixels[4:], offset=6))  # gap
    assert not canvas.complete


def test_continuation_must_match_the_buffer_allocated_at_offset_zero() -> None:
    """Not merely its own total_size claim — that is attacker-chosen too."""
    canvas = dm._MirrorCanvas()
    canvas.handle(_rect_frame(4, 4, (0, 0, 2, 2), _le565(RED) * 2))
    # Consistent within itself (8x1 rect = 16 bytes) but not the frame in flight.
    canvas.handle(_rect_frame(8, 4, (0, 0, 8, 1), _le565(GREEN) * 4, offset=4, total=16))
    assert not canvas.complete


@pytest.mark.parametrize(
    ("panel", "rect"),
    [
        ((4, 4), (3, 0, 2, 2)),  # runs off the right edge
        ((4, 4), (0, 3, 2, 2)),  # runs off the bottom
        ((4, 4), (0, 0, 8, 2)),  # wider than the panel
        ((4, 4), (65535, 0, 2, 2)),  # x + w would overflow a naive check
    ],
)
def test_rect_outside_the_panel_is_rejected(
    panel: tuple[int, int], rect: tuple[int, int, int, int]
) -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(_rect_frame(panel[0], panel[1], rect, _le565(RED) * (rect[2] * rect[3])))
    assert not canvas.complete


def test_rect_total_size_must_equal_its_geometry() -> None:
    canvas = dm._MirrorCanvas()
    # 2x2 RGB565 is 8 bytes; claiming 6 must not allocate or blit.
    canvas.handle(_rect_frame(4, 4, (0, 0, 2, 2), _le565(RED) * 3, total=6))
    assert not canvas.complete


def test_oversized_panel_is_rejected_before_allocating() -> None:
    canvas = dm._MirrorCanvas()
    edge = dm._MAX_PANEL_EDGE + 1
    canvas.handle(
        _frame(
            width=edge,
            height=edge,
            fmt=dm.FORMAT_RGB565,
            total=8,
            data=b"\x00" * 8,
            rect=(0, 0, 2, 2),
        )
    )
    assert not canvas.complete
    assert canvas.rgb is None


def test_undecodable_payload_is_dropped_not_raised() -> None:
    canvas = dm._MirrorCanvas()
    canvas.handle(b"\xff\xff\xff")
    assert not canvas.complete


# ── blank detection ────────────────────────────────────────────────────────


def test_blank_detects_any_single_colour_not_just_black() -> None:
    # An inverted or e-ink panel blanks to white, so "all black" is too narrow.
    assert dm._looks_blank(b"\x00\x00\x00" * 16)
    assert dm._looks_blank(b"\xff\xff\xff" * 16)
    assert not dm._looks_blank(b"\x00\x00\x00" * 15 + b"\xff\xff\xff")


# ── colour palette ─────────────────────────────────────────────────────────


def _region(x: int, y: int, w: int, h: int, on: int, off: int) -> bytes:
    body = (
        dm.encode_varint_field(dm._R_X, x)
        + dm.encode_varint_field(dm._R_Y, y)
        + dm.encode_varint_field(dm._R_WIDTH, w)
        + dm.encode_varint_field(dm._R_HEIGHT, h)
        + dm.encode_varint_field(dm._R_ON_COLOR, on)
        + dm.encode_varint_field(dm._R_OFF_COLOR, off)
    )
    return _bytes_field(dm._P_REGIONS, body)


def _palette(
    *,
    signature: int = 7,
    default_on: int = 0xFFFF,
    default_off: int = 0x0000,
    offset: int = 0,
    total: int = 1,
    regions: bytes = b"",
) -> bytes:
    return (
        dm.encode_varint_field(dm._P_SIGNATURE, signature)
        + dm.encode_varint_field(dm._P_DEFAULT_ON, default_on)
        + dm.encode_varint_field(dm._P_DEFAULT_OFF, default_off)
        + dm.encode_varint_field(dm._P_REGION_OFFSET, offset)
        + dm.encode_varint_field(dm._P_REGION_TOTAL, total)
        + regions
    )


def _mono_frame(signature: int = 7) -> bytes:
    # 8x8, every pixel set, naming the palette it was painted with.
    return _frame(
        width=8,
        height=8,
        fmt=dm.FORMAT_MONO_VLSB,
        total=8,
        data=b"\xff" * 8,
    ) + dm.encode_varint_field(dm._F_PALETTE_SIGNATURE, signature)


def test_palette_colorizes_set_pixels_inside_its_region() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(regions=_region(0, 0, 4, 8, RED, GREEN)))
    canvas.handle(_mono_frame())

    assert canvas.colorized
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\xff\x00\x00"  # inside the region, pixel set -> on_color
    assert rgb[12:15] == b"\xff\xff\xff"  # outside it -> palette default_on


def test_palette_clear_pixels_take_the_region_off_colour() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(regions=_region(0, 0, 8, 8, RED, GREEN)))
    # Every pixel clear.
    canvas.handle(
        _frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=8, data=b"\x00" * 8)
        + dm.encode_varint_field(dm._F_PALETTE_SIGNATURE, 7)
    )
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\x00\xff\x00"


def test_higher_indexed_region_wins_where_regions_overlap() -> None:
    # The proto's precedence rule: later table index overrides earlier.
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(
        _palette(total=2, regions=_region(0, 0, 8, 8, RED, 0) + _region(0, 0, 4, 8, GREEN, 0))
    )
    canvas.handle(_mono_frame())
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\x00\xff\x00"  # inside both -> the later region
    assert rgb[15:18] == b"\xff\x00\x00"  # inside only the first


def test_palette_arriving_after_the_frame_still_colorizes() -> None:
    """Rendering is deferred, so an interleaved palette is not missed."""
    canvas = dm._MirrorCanvas()
    canvas.handle(_mono_frame())
    canvas.palette.handle(_palette(regions=_region(0, 0, 8, 8, RED, 0)))

    assert canvas.colorized
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\xff\x00\x00"


def test_frame_naming_an_unheld_palette_renders_monochrome() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(signature=99, regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.handle(_mono_frame(signature=7))  # references a different palette

    assert not canvas.colorized
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\xff\xff\xff"


def test_frame_without_a_palette_signature_renders_monochrome() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.handle(_frame(width=8, height=8, fmt=dm.FORMAT_MONO_VLSB, total=8, data=b"\xff" * 8))

    assert not canvas.colorized  # signature 0 == device renders monochrome
    rgb = canvas.render()
    assert rgb is not None
    assert rgb[0:3] == b"\xff\xff\xff"


def test_half_received_palette_does_not_colorize() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(total=2, regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.handle(_mono_frame())

    assert not canvas.palette.complete
    assert not canvas.colorized  # would colour some regions and not others


def test_palette_chunks_reassemble_in_order() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(total=2, regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.palette.handle(_palette(total=2, offset=1, regions=_region(0, 0, 4, 8, GREEN, 0)))

    assert canvas.palette.complete
    assert len(canvas.palette.regions) == 2


def test_out_of_sequence_palette_chunk_discards_the_table() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(total=3, regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.palette.handle(_palette(total=3, offset=2, regions=_region(0, 0, 4, 8, GREEN, 0)))

    assert not canvas.palette.complete
    assert canvas.palette.regions == []


def test_palette_chunk_for_a_different_signature_is_dropped() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(_palette(total=2, regions=_region(0, 0, 8, 8, RED, 0)))
    canvas.palette.handle(
        _palette(signature=8, total=2, offset=1, regions=_region(0, 0, 4, 8, GREEN, 0))
    )

    assert canvas.palette.regions == []


def test_palette_over_the_region_cap_is_rejected() -> None:
    canvas = dm._MirrorCanvas()
    canvas.palette.handle(
        _palette(total=dm._MAX_PALETTE_REGIONS + 1, regions=_region(0, 0, 8, 8, RED, 0))
    )
    assert canvas.palette.regions == []


# ── upscaling for documentation shots ──────────────────────────────────────


def test_upscale_is_a_no_op_at_factor_one() -> None:
    rgb = b"\xff\x00\x00\x00\xff\x00"
    assert dm.upscale(rgb, 2, 1, 1) == (rgb, 2, 1)


def test_upscale_replicates_each_pixel_into_a_block() -> None:
    # 2x1 red|green at 2x -> 4x2, every pixel doubled in both axes.
    rgb = b"\xff\x00\x00" + b"\x00\xff\x00"
    out, w, h = dm.upscale(rgb, 2, 1, 2)
    assert (w, h) == (4, 2)
    row = b"\xff\x00\x00" * 2 + b"\x00\xff\x00" * 2
    assert out == row * 2  # both output rows identical


def test_upscale_keeps_edges_hard() -> None:
    """Nearest-neighbour: no interpolated pixels between the two colours."""
    out, _, _ = dm.upscale(b"\x00\x00\x00" + b"\xff\xff\xff", 2, 1, 4)
    colours = {out[i : i + 3] for i in range(0, len(out), 3)}
    assert colours == {b"\x00\x00\x00", b"\xff\xff\xff"}
