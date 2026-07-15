"""Inject packets into a locally-connected board as if they arrived off the LoRa radio.

Requires the target to run firmware built with ``-D MESHTASTIC_ENABLE_FRAME_INJECTION=1``
(portduino sim nodes support it unconditionally). A crafted frame rides inside a ``Compressed``
envelope wrapped in a ``MeshPacket`` sent on the ``SIMULATOR_APP`` portnum (69); the firmware
unwraps it and delivers it through the real receive pipeline, so it gets ``from!=0`` enforcement,
channel/PKC decryption, hop handling, dedup, and module dispatch - like an over-the-air packet.

    Compressed.portnum == UNKNOWN_APP -> Compressed.data is verbatim CIPHERTEXT (firmware decrypts)
    Compressed.portnum == <portnum>   -> Compressed.data is the DECODED payload for that portnum

Firmware seam: ``MeshService::injectAsReceived`` (src/mesh/MeshService.cpp).
"""

from __future__ import annotations

import random
import struct
from typing import Any

from meshtastic import admin_pb2, mesh_pb2, portnums_pb2

from .connection import connect


def _require_confirm(confirm: bool) -> None:
    if not confirm:
        raise ValueError("inject_frame forges over-the-air traffic and requires confirm=True.")


# Public "default" PSK family (src/mesh/Channels.h). 1-byte PSK index N -> this, last byte += N-1.
DEFAULT_PSK = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59, 0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)
SIMULATOR_APP = 69
UNKNOWN_APP = 0

# Modem-preset enum -> the long display name Channels::getName() hashes for an empty channel name.
PRESET_LONGNAME = {
    0: "LongFast",
    1: "LongSlow",
    2: "LongMod",
    3: "MediumSlow",
    4: "MediumFast",
    5: "ShortSlow",
    6: "ShortFast",
    7: "LongTurbo",
    8: "ShortTurbo",
    9: "MediumTurbo",
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


def _aes_ctr(key: bytes, from_node: int, packet_id: int, data: bytes) -> bytes:
    """Meshtastic channel crypto: AES-CTR, IV = packetId(8 LE) | fromNode(4 LE) | 0(4)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # cryptography is an optional extra (see pyproject `[inject]`)
        raise RuntimeError(
            "encrypted injection needs the 'cryptography' package: "
            "pip install 'meshtastic-mcp[inject]' (or use encrypt=false / a raw ciphertext)"
        ) from e
    nonce = struct.pack("<QII", packet_id & 0xFFFFFFFFFFFFFFFF, from_node & 0xFFFFFFFF, 0)
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return enc.update(data) + enc.finalize()


def _resolve_channel(iface, ch_index: int) -> tuple[str, bytes, int]:
    ch = iface.localNode.getChannelByChannelIndex(ch_index)
    if ch is None:
        raise ValueError(f"channel index {ch_index} is not configured on this node")
    key = _expand_psk(bytes(ch.settings.psk))
    name = ch.settings.name
    if not name:
        lc = iface.localNode.localConfig.lora
        name = PRESET_LONGNAME.get(int(lc.modem_preset), "LongFast") if lc.use_preset else "Custom"
    return name, key, _channel_hash(name, key)


def _build_data(portnum: int, payload: bytes, want_response: bool) -> bytes:
    d = mesh_pb2.Data()
    d.portnum = portnum
    d.payload = payload
    if want_response:
        d.want_response = True
    return d.SerializeToString()


def _rand_id() -> int:
    return random.getrandbits(32) or 1


def _send(
    iface,
    *,
    from_node,
    to_node,
    packet_id,
    ch_hash,
    inner_portnum,
    inner_bytes,
    encrypted,
    pki_encrypted=False,
    public_key=b"",
    want_ack=False,
    hop_limit=3,
) -> dict[str, Any]:
    comp = mesh_pb2.Compressed()
    comp.portnum = UNKNOWN_APP if encrypted else inner_portnum
    comp.data = inner_bytes

    from_node &= 0xFFFFFFFF  # wrap to uint32 like replay/build.py, so a bad value doesn't raise
    to_node &= 0xFFFFFFFF
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", from_node)  # 'from' is a Python keyword
    mp.to = to_node
    mp.id = packet_id
    mp.channel = ch_hash & 0xFF
    mp.want_ack = want_ack
    mp.hop_limit = hop_limit
    mp.hop_start = hop_limit
    if pki_encrypted:
        mp.pki_encrypted = True
        if public_key:
            mp.public_key = public_key
    mp.decoded.portnum = SIMULATOR_APP
    mp.decoded.payload = comp.SerializeToString()

    tr = mesh_pb2.ToRadio()
    tr.packet.CopyFrom(mp)
    iface._sendToRadio(tr)
    return {
        "from": f"0x{from_node:08x}",
        "to": f"0x{to_node:08x}",
        "id": f"0x{packet_id:08x}",
        "channel_hash": mp.channel,
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
) -> dict[str, Any]:
    """Craft frame(s) and inject via SIMULATOR_APP. See module docstring for the wire format."""
    _require_confirm(confirm)
    frm = int(from_node, 0) if isinstance(from_node, str) else int(from_node)
    pubkey = None
    if public_key_b64:
        import base64

        pubkey = base64.b64decode(public_key_b64)

    with connect(port=port) as iface:
        my_num = iface.getMyNodeInfo()["num"]
        to_node = (int(to, 0) if isinstance(to, str) else int(to)) if to is not None else my_num
        name, key, chash = _resolve_channel(iface, channel_index)

        def _pid() -> int:
            if packet_id is None:  # not `if packet_id` - an explicit 0/"0x0" is a valid id
                return _rand_id()
            return int(packet_id, 0) if isinstance(packet_id, str) else int(packet_id)

        def _inject_payload(pn: int, payload: bytes) -> dict[str, Any]:
            pid = _pid()
            if encrypt:
                if not key:
                    raise ValueError(
                        "channel has no key; set encrypt=false or target a keyed channel"
                    )
                inner = _aes_ctr(key, frm, pid, _build_data(pn, payload, want_response))
            else:
                inner = payload
            return _send(
                iface,
                from_node=frm,
                to_node=to_node,
                packet_id=pid,
                ch_hash=chash,
                inner_portnum=pn,
                inner_bytes=inner,
                encrypted=encrypt,
                pki_encrypted=pki,
                public_key=pubkey or b"",
                want_ack=want_response,
            )

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
            pid = _pid()
            sent = [
                _send(
                    iface,
                    from_node=frm,
                    to_node=to_node,
                    packet_id=pid,
                    ch_hash=chash,
                    inner_portnum=UNKNOWN_APP,
                    inner_bytes=bytes.fromhex(ciphertext_hex),
                    encrypted=True,
                    pki_encrypted=pki,
                    public_key=pubkey or b"",
                )
            ]
        elif mode == "fuzz":
            rng = random.Random(fuzz_seed)
            sent = []
            for i in range(fuzz_count):
                blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 240)))
                sent.append(
                    _send(
                        iface,
                        from_node=frm or (0x1000 + i),
                        to_node=to_node,
                        packet_id=_rand_id(),
                        ch_hash=rng.randint(0, 255),
                        inner_portnum=UNKNOWN_APP,
                        inner_bytes=blob,
                        encrypted=True,
                    )
                )
        else:
            raise ValueError(f"unknown mode {mode!r}")

    return {"ok": True, **target, "injected": len(sent), "frames": sent}
