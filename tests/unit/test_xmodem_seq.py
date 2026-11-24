# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""XModem block numbering for the hardware NodeDB push (meshtastic/meshtastic-mcp#227).

The firmware counts blocks in a uint16, so block 256 follows 255. Wrapping at 256
sent seq=0 mid-transfer, which the firmware reads as a new transfer and NAKs, so
every file over 255 blocks (the 250-node fixture is 271) failed.
"""

from __future__ import annotations

from meshtastic_mcp.fixtures import _XMODEM_CHUNK, _xmodem_seq


def test_first_data_block_is_one() -> None:
    assert _xmodem_seq(0) == 1


def test_block_after_255_is_256_not_zero() -> None:
    assert _xmodem_seq(254 * _XMODEM_CHUNK) == 255
    assert _xmodem_seq(255 * _XMODEM_CHUNK) == 256


def test_a_271_block_file_never_sends_seq_zero() -> None:
    seqs = [_xmodem_seq(o) for o in range(0, 271 * _XMODEM_CHUNK, _XMODEM_CHUNK)]
    assert seqs == list(range(1, 272))


def test_wraps_like_the_firmware_uint16() -> None:
    assert _xmodem_seq(65534 * _XMODEM_CHUNK) == 65535
    assert _xmodem_seq(65535 * _XMODEM_CHUNK) == 0
