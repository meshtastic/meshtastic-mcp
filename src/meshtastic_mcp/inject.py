# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Inject packets into a locally-connected board as if they arrived off the LoRa radio.

Requires the target to run firmware built with ``-D MESHTASTIC_ENABLE_FRAME_INJECTION=1``
(portduino sim nodes support it unconditionally). A crafted frame rides inside an ``InjectedFrame``
envelope wrapped in a ``MeshPacket`` sent on the ``SIMULATOR_APP`` portnum (68); the firmware
unwraps it and delivers it through the real receive pipeline, so it gets ``from!=0`` enforcement,
channel/PKC decryption, hop handling, dedup, and module dispatch - like an over-the-air packet.

    InjectedFrame.portnum == UNKNOWN_APP -> .data is verbatim CIPHERTEXT (firmware decrypts)
    InjectedFrame.portnum == <portnum>   -> .data is the DECODED payload for that portnum

3.0 channel encryption is AES-CCM with a 4-byte tag over the frame's authenticated header, and only
a broadcast frame is channel-encrypted: a frame addressed to one node is PKI. So an encrypted
channel frame goes to the broadcast address, and names its channel by ``channel_hash``.

Firmware seam: ``MeshService::injectAsReceived`` (src/mesh/MeshService.cpp).
"""

from __future__ import annotations

import random
import struct
from typing import Any

from meshtastic import admin_pb2, api_pb2, packet_pb2, portnums_pb2, util, wire_pb2

from .connection import connect


def _require_confirm(confirm: bool) -> None:
    if not confirm:
        raise ValueError("inject_frame forges over-the-air traffic and requires confirm=True.")


# Public "default" PSK family (src/mesh/Channels.h). 1-byte PSK index N -> this, last byte += N-1.
DEFAULT_PSK = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59, 0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)
SIMULATOR_APP = portnums_pb2.PortNum.SIMULATOR_APP
UNKNOWN_APP = 0
BROADCAST = 0xFFFFFFFF
CHANNEL_TAG_LEN = 4  # AES-CCM tag on a 3.0 channel frame

# ModemPreset enum -> the long display name Channels::getName() hashes for an empty channel name.
PRESET_LONGNAME = {
    0: "LongFast",
    1: "MediumSlow",
    2: "MediumFast",
    3: "ShortSlow",
    4: "ShortFast",
    5: "LongMod",
    6: "ShortTurbo",
    7: "LongTurbo",
    8: "LiteFast",
    9: "LiteSlow",
    10: "NarrowFast",
    11: "NarrowSlow",
    12: "TinyFast",
    13: "TinySlow",
    14: "MediumTurbo",
}


def _xor_hash(b: bytes) -> int:
    h = 0
    for x in b:
        h ^= x
    return h


def _expand_psk(psk: bytes) -> bytes:
    """Mirror Channels::getKey expansion. Returns b'' for 'no encryption'."""
    if len(psk) == 0:
        return b""
    if len(psk) == 1:
        idx = psk[0]
        if idx == 0:
            return b""
        k = bytearray(DEFAULT_PSK)
        k[-1] = (k[-1] + idx - 1) & 0xFF
        return bytes(k)
    if len(psk) < 16:  # firmware pads a short AES128 key with zeros
        return psk + b"\x00" * (16 - len(psk))
    if len(psk) < 32 and len(psk) != 16:  # firmware pads a 17-31 byte key up to AES256
        return psk + b"\x00" * (32 - len(psk))
    return psk


def _channel_hash(name: str, key: bytes) -> int:
    return _xor_hash(name.encode()) ^ _xor_hash(key)


def channel_aad(mp: Any) -> bytes:
    """A broadcast frame's authenticated header (firmware WireFrame::buildAad): ctrl and flags with
    the bits relays rewrite zeroed, from, id, the channel hash, and the options block."""
    flags = (mp.hop_start & 0x0F) << 4
    flags |= mp.flags & (
        packet_pb2.MeshPacket.PACKET_WANT_ACK | packet_pb2.MeshPacket.PACKET_RECORD_PATH
    )
    aad = bytes(
        [0x01, flags | (0x02 if mp.header_options else 0)]
    )  # ctrl: profile BCAST, hop_limit 0
    aad += struct.pack("<II", getattr(mp, "from"), mp.id) + bytes([mp.channel_hash & 0xFF])
    if mp.header_options:
        aad += bytes([len(mp.header_options)]) + mp.header_options
    return aad


def channel_encrypt(key: bytes, mp: Any, data: bytes) -> bytes:
    """3.0 channel crypto: AES-CCM, 4-byte tag, nonce packetId(8 LE) | fromNode(4 LE) | 0, over the
    frame's authenticated header. Returns ciphertext followed by the tag."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    except ImportError as e:  # cryptography is an optional extra (see pyproject `[inject]`)
        raise RuntimeError(
            "encrypted injection needs the 'cryptography' package: "
            "pip install 'meshtastic-mcp[inject]' (or use encrypt=false / a raw ciphertext)"
        ) from e
    nonce = struct.pack("<QI", mp.id, getattr(mp, "from")) + b"\x00"
    return AESCCM(key, tag_length=CHANNEL_TAG_LEN).encrypt(nonce, data, channel_aad(mp))


def _resolve_channel(iface, ch_index: int) -> tuple[str, bytes, int]:
    ch = iface.localNode.getChannelByChannelIndex(ch_index)
    if ch is None:
        raise ValueError(f"channel index {ch_index} is not configured on this node")
    key = _expand_psk(bytes(ch.settings.psk))
    name = ch.settings.name
    if not name:
        lc = iface.localNode.localConfig.lora
        use_preset = lc.flags & lc.LORA_USE_PRESET
        name = PRESET_LONGNAME.get(int(lc.modem_preset), "LongFast") if use_preset else "Custom"
    return name, key, _channel_hash(name, key)


def _build_data(portnum: int, payload: bytes, want_response: bool) -> bytes:
    d = wire_pb2.Data()
    d.portnum = portnum
    d.payload = payload
    if want_response:
        d.bitfield = wire_pb2.Data.BITFIELD_WANT_RESPONSE
    return d.SerializeToString()


def _rand_id() -> int:
    return random.getrandbits(32) or 1


def _header(
    *,
    from_node: int,
    to_node: int,
    packet_id: int,
    ch_hash: int,
    pki_encrypted: bool = False,
    public_key: bytes = b"",
    want_ack: bool = False,
    hop_limit: int = 3,
    header_options: bytes = b"",
) -> Any:
    """The injected MeshPacket's header fields; the frame itself goes in by `_send`."""
    mp = packet_pb2.MeshPacket()
    setattr(
        mp, "from", from_node & 0xFFFFFFFF
    )  # 'from' is a Python keyword; wrap like replay/build.py
    mp.to = to_node & 0xFFFFFFFF
    mp.id = packet_id
    mp.channel_hash = ch_hash & 0xFF
    if want_ack:
        mp.flags |= packet_pb2.MeshPacket.PACKET_WANT_ACK
    mp.hop_limit = hop_limit
    mp.hop_start = hop_limit
    mp.header_options = header_options  # in the AAD, so set before any encryption
    if pki_encrypted:
        mp.flags |= packet_pb2.MeshPacket.PACKET_PKI_ENCRYPTED
        if public_key:
            mp.public_key = public_key
    return mp


def _send(
    iface: Any,
    mp: Any,
    *,
    inner_portnum: int,
    inner_bytes: bytes,
    encrypted: bool,
) -> dict[str, Any]:
    frame = wire_pb2.InjectedFrame()
    frame.portnum = UNKNOWN_APP if encrypted else inner_portnum  # type: ignore[assignment]
    frame.data = inner_bytes
    mp.decoded.portnum = SIMULATOR_APP
    mp.decoded.payload = frame.SerializeToString()

    tr = api_pb2.ToRadio()
    tr.packet.CopyFrom(mp)
    iface._sendToRadio(tr)
    pki_encrypted = bool(mp.flags & packet_pb2.MeshPacket.PACKET_PKI_ENCRYPTED)
    return {
        "from": f"0x{getattr(mp, 'from'):08x}",
        "to": f"0x{mp.to:08x}",
        "id": f"0x{mp.id:08x}",
        "channel_hash": mp.channel_hash,
        "portnum": inner_portnum,
        "bytes": len(inner_bytes),
        "encrypted": encrypted,
        "pki_encrypted": pki_encrypted,
    }


def inject_frame(
    mode: str = "text",
    body: str | None = None,
    portnum: int | None = None,
    payload_hex: str = "",
    ciphertext_hex: str = "",
    long_name: str = "INJECTED",
    short_name: str = "INJ",
    session_hex: str = "",
    from_node: str | int = "0xdeadbeef",
    to: str | int | None = None,
    channel_index: int = 0,
    packet_id: str | int | None = None,
    want_response: bool = False,
    encrypt: bool = True,
    pki: bool = False,
    public_key_b64: str | None = None,
    fuzz_count: int = 10,
    fuzz_seed: int = 1,
    confirm: bool = False,
    port: str | None = None,
    header_options_hex: str = "",
    scope_region: str | None = None,
) -> dict[str, Any]:
    """Craft frame(s) and inject via SIMULATOR_APP. See module docstring for the wire format."""
    _require_confirm(confirm)
    options = bytes.fromhex(header_options_hex)
    frm = int(from_node, 0) if isinstance(from_node, str) else int(from_node)
    pubkey = None
    if public_key_b64:
        import base64

        pubkey = base64.b64decode(public_key_b64)

    with connect(port=port) as iface:
        my_num = iface.getMyNodeInfo()["num"]
        channel_frame = encrypt and not pki and mode in ("text", "raw", "admin")
        if to is not None:
            to_node = int(to, 0) if isinstance(to, str) else int(to)
        else:
            to_node = BROADCAST if channel_frame else my_num
        if channel_frame and to_node != BROADCAST:
            raise ValueError(
                "3.0 channel encryption is broadcast only; a frame to one node is PKI "
                "(pki=true, encrypt=false), or omit `to`"
            )
        name, key, chash = _resolve_channel(iface, channel_index)

        def _pid() -> int:
            if packet_id is None:  # not `if packet_id` - an explicit 0/"0x0" is a valid id
                return _rand_id()
            return int(packet_id, 0) if isinstance(packet_id, str) else int(packet_id)

        def _options(pid: int) -> bytes:
            # The code a node with this home region appends to its broadcast (SCHEMA.md section 8)
            if not scope_region:
                return options
            if not channel_frame:
                raise ValueError("a scope code belongs on a channel broadcast")
            region = util.canonical_region_name(scope_region)
            code = util.scope_code(region, chash, frm, pid)
            return options + wire_pb2.HeaderOptions(scope_code=code).SerializeToString()

        def _inject_payload(pn: int, payload: bytes) -> dict[str, Any]:
            pid = _pid()
            mp = _header(
                from_node=frm,
                to_node=to_node,
                packet_id=pid,
                ch_hash=chash,
                pki_encrypted=pki,
                public_key=pubkey or b"",
                want_ack=want_response,
                header_options=_options(pid),
            )
            if encrypt:
                if not key:
                    raise ValueError(
                        "channel has no key; set encrypt=false or target a keyed channel"
                    )
                inner = channel_encrypt(key, mp, _build_data(pn, payload, want_response))
            else:
                inner = payload
            return _send(iface, mp, inner_portnum=pn, inner_bytes=inner, encrypted=encrypt)

        target = {
            "target": f"0x{my_num:08x}",
            "channel": name,
            "channel_hash": chash,
            "keylen": len(key),
        }

        if mode == "text":
            sent = [_inject_payload(portnums_pb2.PortNum.TEXT_MESSAGE_APP, (body or "").encode())]
        elif mode == "raw":
            if portnum is None:
                raise ValueError("mode=raw requires portnum")
            sent = [_inject_payload(int(portnum), bytes.fromhex(payload_hex))]
        elif mode == "admin":
            am = admin_pb2.AdminMessage()
            am.set_owner.long_name = long_name
            am.set_owner.short_name = short_name
            if session_hex:
                am.session_passkey = bytes.fromhex(session_hex)
            sent = [_inject_payload(portnums_pb2.PortNum.ADMIN_APP, am.SerializeToString())]
        elif mode == "ciphertext":
            mp = _header(
                from_node=frm,
                to_node=to_node,
                packet_id=_pid(),
                ch_hash=chash,
                pki_encrypted=pki,
                public_key=pubkey or b"",
                header_options=options,
            )
            sent = [
                _send(
                    iface,
                    mp,
                    inner_portnum=UNKNOWN_APP,
                    inner_bytes=bytes.fromhex(ciphertext_hex),
                    encrypted=True,
                )
            ]
        elif mode == "fuzz":
            rng = random.Random(fuzz_seed)
            sent = []
            for i in range(fuzz_count):
                blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 240)))
                mp = _header(
                    from_node=frm or (0x1000 + i),
                    to_node=to_node,
                    packet_id=_rand_id(),
                    ch_hash=rng.randint(0, 255),
                )
                sent.append(
                    _send(iface, mp, inner_portnum=UNKNOWN_APP, inner_bytes=blob, encrypted=True)
                )
        else:
            raise ValueError(f"unknown mode {mode!r}")

    return {"ok": True, **target, "injected": len(sent), "frames": sent}
