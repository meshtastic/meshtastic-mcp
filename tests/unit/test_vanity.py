# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Vanity NodeNum / colour derivation, hit parsing, and the identity write.

No GPU and no radio: the X25519 ladder is checked against the RFC 7748 vectors,
the identity formula against a real mvgrind hit captured on an Apple M4
(`!dead5d54`, cross-checked at the time against mvgrind's own CPU re-derivation),
and `apply_key` against a fake node built on the real protobufs — which is where
the load-bearing detail lives: the write must CLEAR `public_key`, or the firmware
takes neither keygen branch and the NodeNum silently never moves.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager

import pytest

from meshtastic_mcp import vanity

# RFC 7748 §6.1 — Alice's and Bob's keypairs.
RFC7748 = [
    (
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a",
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a",
    ),
    (
        "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb",
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f",
    ),
]

# A real mvgrind hit: `mvgrind dead`. The full chain in one vector —
# private key -> public key -> crc32 -> node id -> the colour the apps paint.
HIT_SK = "78add2dbefef3cc4adb4b93e7f0e25cc72101c995707ab05055528ea8854116e"
HIT_PK = "66a3554c5f5ed575c0745a41bcb3b30de05d5f68438b6b9f46c8715902ed2145"
HIT_ID = "!dead5d54"
HIT_COLOR = "#ad5d54"

FOUND_FILE = f"""node_id={HIT_ID}
app_color={HIT_COLOR}
private_key_hex={HIT_SK}
public_key_hex={HIT_PK}
private_key_b64={base64.b64encode(bytes.fromhex(HIT_SK)).decode()}
public_key_b64={base64.b64encode(bytes.fromhex(HIT_PK)).decode()}

"""


# ---------------------------------------------------------------------------
# Curve + identity derivation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("sk", "pk"), RFC7748)
def test_x25519_matches_rfc7748(sk: str, pk: str) -> None:
    assert vanity.x25519_public_key(bytes.fromhex(sk)).hex() == pk


def test_identity_chain_matches_a_real_grind() -> None:
    desc = vanity.describe_key(HIT_SK)
    assert desc["public_key_hex"] == HIT_PK
    assert desc["node_id"] == HIT_ID
    assert desc["nodenum"] == int(HIT_ID[1:], 16)
    assert desc["color"]["hex"] == HIT_COLOR
    assert desc["clamped"] is True


def test_nodenum_is_crc32_of_the_public_key() -> None:
    # The firmware's formula (NodeDB.cpp::createNewIdentity), spelled out so a
    # change to either side of it fails here rather than on a radio.
    import zlib

    pk = bytes.fromhex(HIT_PK)
    assert vanity.nodenum_of_public_key(pk) == zlib.crc32(pk) & 0xFFFFFFFF
    assert vanity.node_id(vanity.nodenum_of_public_key(pk)) == HIT_ID


def test_color_reads_the_low_24_bits_as_rgb() -> None:
    # Mirrors Meshtastic-Android's nodeColorsFromNum, foreground included.
    color = vanity.node_color(0x8ADC143C)
    assert color["hex"] == "#dc143c"  # crimson
    assert color["rgb"] == [0xDC, 0x14, 0x3C]
    assert color["foreground"] == "white"
    assert vanity.node_color(0x00FFFFFF)["foreground"] == "black"


def test_clamp_round_trip() -> None:
    raw = bytes([0xFF] * 32)
    assert not vanity.is_clamped(raw)
    clamped = vanity.clamp(raw)
    assert vanity.is_clamped(clamped)
    assert vanity.clamp(clamped) == clamped


def test_unclamped_key_is_reported_not_silently_fixed() -> None:
    raw = bytes([0xFF] * 32)
    assert vanity.describe_key(raw.hex())["clamped"] is False


@pytest.mark.parametrize(
    "text",
    [HIT_SK, HIT_SK.upper(), base64.b64encode(bytes.fromhex(HIT_SK)).decode()],
)
def test_parse_private_key_accepts_hex_and_base64(text: str) -> None:
    assert vanity.parse_private_key(text) == bytes.fromhex(HIT_SK)


@pytest.mark.parametrize("text", ["", "zz", HIT_SK[:-2], base64.b64encode(b"short").decode()])
def test_parse_private_key_rejects_junk(text: str) -> None:
    with pytest.raises(vanity.VanityError):
        vanity.parse_private_key(text)


# ---------------------------------------------------------------------------
# mvgrind output parsing — every hit is re-derived here, not trusted
# ---------------------------------------------------------------------------
def test_parse_hits_verifies_against_its_own_derivation(tmp_path) -> None:
    out = tmp_path / "found.txt"
    out.write_text(FOUND_FILE, encoding="utf-8")
    (hit,) = vanity.parse_hits(out)
    assert hit["node_id"] == HIT_ID
    assert hit["verified"] is True
    assert hit["private_key_hex"] == HIT_SK


def test_parse_hits_flags_a_hit_that_lies_about_its_id(tmp_path) -> None:
    out = tmp_path / "found.txt"
    out.write_text(FOUND_FILE.replace(HIT_ID, "!deadbeef"), encoding="utf-8")
    (hit,) = vanity.parse_hits(out)
    assert hit["verified"] is False
    assert hit["node_id"] == HIT_ID  # what the key ACTUALLY produces
    assert hit["reported_node_id"] == "!deadbeef"


def test_parse_hits_on_a_missing_or_empty_file(tmp_path) -> None:
    assert vanity.parse_hits(tmp_path / "nope.txt") == []
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert vanity.parse_hits(empty) == []


# ---------------------------------------------------------------------------
# Argument validation — a pattern must never be readable as an mvgrind flag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", ["-h", "--color", "dc80;rm -rf /", "dc80 --bench 9", "xyz"])
def test_grind_rejects_a_pattern_that_is_not_a_pattern(pattern: str) -> None:
    with pytest.raises(vanity.VanityError):
        vanity.grind_start(pattern=pattern)


@pytest.mark.parametrize("pattern", ["dc80", "!dc801051", "dc80****", "dc80,801f,d0f0", "d?.0"])
def test_valid_patterns_pass_validation(pattern: str) -> None:
    vanity._validate(pattern, None, 0)


@pytest.mark.parametrize("color", ["-crimson", "#gg0000", "#dc143", "rgb(1,2,3)"])
def test_grind_rejects_a_bad_color(color: str) -> None:
    with pytest.raises(vanity.VanityError):
        vanity.grind_start(color=color)


def test_grind_needs_something_to_grind_for() -> None:
    with pytest.raises(vanity.VanityError):
        vanity.grind_start()


# ---------------------------------------------------------------------------
# apply_key — the identity write
# ---------------------------------------------------------------------------
class FakeNode:
    def __init__(self, region: int) -> None:
        from meshtastic.protobuf import localonly_pb2

        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.region = region
        self.localConfig.security.private_key = bytes(32)
        self.localConfig.security.public_key = bytes(32)
        self.written: list[str] = []

    def writeConfig(self, name: str) -> None:
        self.written.append(name)


class FakeIface:
    def __init__(self, region: int, node_num: int) -> None:
        self.localNode = FakeNode(region)
        self.myInfo = type("MyInfo", (), {"my_node_num": node_num})()


@contextmanager
def _fake_connect(iface: FakeIface):
    yield iface


def _patch_connect(monkeypatch, iface: FakeIface) -> None:
    monkeypatch.setattr(
        vanity.connection, "connect", lambda *a, **k: _fake_connect(iface), raising=True
    )


def test_apply_requires_confirm() -> None:
    with pytest.raises(vanity.VanityError, match="confirm=True"):
        vanity.apply_key(HIT_SK, confirm=False)


def test_apply_refuses_an_unclamped_key() -> None:
    with pytest.raises(vanity.VanityError, match="not clamped"):
        vanity.apply_key(bytes([0xFF] * 32).hex(), confirm=True)


def test_apply_refuses_when_the_region_is_unset(monkeypatch) -> None:
    iface = FakeIface(region=0, node_num=0x11111111)
    _patch_connect(monkeypatch, iface)
    with pytest.raises(vanity.VanityError, match="UNSET"):
        vanity.apply_key(HIT_SK, confirm=True, verify=False)
    assert iface.localNode.written == []


def test_apply_clears_the_public_key_so_the_firmware_re_derives_it(monkeypatch) -> None:
    # The whole trap: AdminModule only calls generateCryptoKeyPair(private_key)
    # when the incoming public_key is EMPTY. Echo the old 32-byte public key
    # back and neither keygen branch fires — the node keeps its old NodeNum.
    iface = FakeIface(region=1, node_num=0x11111111)
    _patch_connect(monkeypatch, iface)
    result = vanity.apply_key(HIT_SK, confirm=True, verify=False)

    sec = iface.localNode.localConfig.security
    assert iface.localNode.written == ["security"]
    assert sec.private_key == bytes.fromhex(HIT_SK)
    assert len(sec.public_key) == 0
    assert result["changed"] is True
    assert result["node_id"] == HIT_ID
    assert result["previous_node_id"] == "!11111111"


def test_apply_is_a_no_op_when_the_device_already_has_that_identity(monkeypatch) -> None:
    iface = FakeIface(region=1, node_num=int(HIT_ID[1:], 16))
    _patch_connect(monkeypatch, iface)
    result = vanity.apply_key(HIT_SK, confirm=True, verify=False)
    assert result["changed"] is False
    assert iface.localNode.written == []
