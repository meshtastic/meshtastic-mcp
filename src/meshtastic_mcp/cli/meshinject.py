#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""
meshinject - inject packets into a locally-connected Meshtastic board as if they arrived off the LoRa
radio. Requires the target to run firmware built with -D MESHTASTIC_ENABLE_FRAME_INJECTION=1 (portduino
sim nodes support it unconditionally).

A crafted frame rides inside a Compressed envelope wrapped in a MeshPacket sent on the SIMULATOR_APP
portnum (69). The firmware (MeshService::injectAsReceived) unwraps it and delivers it through the real
receive pipeline, so it gets from!=0 enforcement, channel/PKC decryption, hop handling, dedup, and
module dispatch - exactly like an over-the-air packet.

  Compressed.portnum == UNKNOWN_APP -> Compressed.data is verbatim CIPHERTEXT (firmware decrypts it)
  Compressed.portnum == <portnum>   -> Compressed.data is the DECODED payload for that portnum

Run with the meshtastic-CLI venv python (has meshtastic + cryptography).
"""

import argparse
import random
import struct
import sys
import time

import meshtastic.serial_interface as ser
import meshtastic.tcp_interface as tcp
from meshtastic import admin_pb2, mesh_pb2, portnums_pb2

# The public "default" PSK family (src/mesh/Channels.h). 1-byte PSK index N -> this with last byte += N-1.
DEFAULT_PSK = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59, 0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)
SIMULATOR_APP = 69
UNKNOWN_APP = 0

# Modem preset enum value -> the long display name Channels::getName() hashes for an empty channel name.
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
    # newer presets (values may shift across versions; name resolved from the device is preferred)
}


def xor_hash(b: bytes) -> int:
    h = 0
    for x in b:
        h ^= x
    return h


def expand_psk(psk: bytes) -> bytes:
    """Mirror Channels::getKey expansion. Returns b'' for 'no encryption'."""
    if len(psk) == 0:
        return b""
    if len(psk) == 1:
        idx = psk[0]
        if idx == 0:
            return b""  # encryption off
        k = bytearray(DEFAULT_PSK)
        k[-1] = (k[-1] + idx - 1) & 0xFF
        return bytes(k)
    if len(psk) < 16:  # firmware zero-pads a too-short key to AES128
        return psk + b"\x00" * (16 - len(psk))
    if len(psk) < 32 and len(psk) != 16:  # firmware pads a 17-31 byte key up to AES256
        return psk + b"\x00" * (32 - len(psk))
    return psk


def channel_hash(name: str, key: bytes) -> int:
    return xor_hash(name.encode()) ^ xor_hash(key)


def aes_ctr(key: bytes, from_node: int, packet_id: int, data: bytes) -> bytes:
    """Meshtastic channel crypto: AES-CTR, IV = packetId(8 LE) | fromNode(4 LE) | 0(4)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = struct.pack("<QII", packet_id & 0xFFFFFFFFFFFFFFFF, from_node & 0xFFFFFFFF, 0)
    algo = algorithms.AES(key)  # 16B->AES128, 32B->AES256
    enc = Cipher(algo, modes.CTR(nonce)).encryptor()
    return enc.update(data) + enc.finalize()


def connect(args):
    if args.serial:
        return ser.SerialInterface(devPath=args.serial)
    host, _, port = (args.host or "localhost").partition(":")
    return tcp.TCPInterface(host, portNumber=int(port) if port else 4403)


def resolve_channel(iface, ch_index):
    """Return (resolved_name, expanded_key, hash_byte) for the given channel index on the target."""
    ch = iface.localNode.getChannelByChannelIndex(ch_index)
    if ch is None:
        raise SystemExit(f"channel index {ch_index} is not configured on this node")
    settings = ch.settings
    key = expand_psk(bytes(settings.psk))
    name = settings.name
    if not name:  # empty -> firmware substitutes the modem-preset long name (if use_preset)
        lc = iface.localNode.localConfig.lora
        name = PRESET_LONGNAME.get(int(lc.modem_preset), "LongFast") if lc.use_preset else "Custom"
    return name, key, channel_hash(name, key)


def send_frame(
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
):
    """Wrap inner_bytes (ciphertext if encrypted else decoded payload) and inject via SIMULATOR_APP."""
    comp = mesh_pb2.Compressed()
    comp.portnum = UNKNOWN_APP if encrypted else inner_portnum
    comp.data = inner_bytes

    from_node &= 0xFFFFFFFF  # wrap to uint32 so a bad value doesn't raise a protobuf range error
    to_node &= 0xFFFFFFFF
    mp = mesh_pb2.MeshPacket()
    mp.__setattr__("from", from_node)  # 'from' is a Python keyword
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
    kind = "encrypted" if encrypted else "decoded"
    print(
        f"injected {kind} frame: from=0x{from_node:08x} to=0x{to_node:08x} id=0x{packet_id:08x} "
        f"ch={mp.channel} portnum={inner_portnum} len={len(inner_bytes)}"
        f"{' pki' if pki_encrypted else ''}"
    )


def rand_id():
    return random.getrandbits(32) or 1


def build_data(portnum, payload, want_response=False):
    d = mesh_pb2.Data()
    d.portnum = portnum
    d.payload = payload
    if want_response:
        d.want_response = True
    return d.SerializeToString()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--serial", help="serial device path (e.g. /dev/cu.usbmodem101)")
    ap.add_argument("--host", help="TCP host or host:port (default localhost:4403)")
    ap.add_argument(
        "--from",
        dest="from_node",
        default="0xdeadbeef",
        help="source NodeNum to forge (hex or int). from==0 is dropped like real RX.",
    )
    ap.add_argument(
        "--to",
        dest="to_node",
        default=None,
        help="destination NodeNum (default: the target's own num)",
    )
    ap.add_argument("--channel-index", type=int, default=0)
    ap.add_argument("--id", dest="pid", default=None, help="packet id (default random)")
    ap.add_argument("--want-response", action="store_true")
    ap.add_argument(
        "--no-encrypt",
        action="store_true",
        help="inject as already-decoded (skip channel encryption); needed with --pki",
    )
    ap.add_argument("--pki", action="store_true", help="mark pki_encrypted and attach --public-key")
    ap.add_argument("--public-key", help="dest/sender public key (base64) for --pki")

    sub = ap.add_subparsers(dest="cmd", required=True)
    p_text = sub.add_parser("text", help="inject a text message")
    p_text.add_argument("body")
    p_raw = sub.add_parser("raw", help="inject arbitrary portnum + payload (hex)")
    p_raw.add_argument("--portnum", type=int, required=True)
    p_raw.add_argument("--payload-hex", default="")
    p_admin = sub.add_parser(
        "admin", help="inject an admin set_owner (reproduces remote-admin scenarios)"
    )
    p_admin.add_argument("--long", default="INJECTED")
    p_admin.add_argument("--short", default="INJ")
    p_admin.add_argument(
        "--session-hex", default="", help="8-byte session_passkey to present (hex)"
    )
    p_cipher = sub.add_parser("ciphertext", help="inject verbatim ciphertext bytes (hex)")
    p_cipher.add_argument("--hex", required=True)
    p_fuzz = sub.add_parser("fuzz", help="inject N random/malformed frames")
    p_fuzz.add_argument("-n", type=int, default=10)
    p_fuzz.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    from_node = int(args.from_node, 0)
    iface = connect(args)
    my_num = iface.getMyNodeInfo()["num"]
    to_node = int(args.to_node, 0) if args.to_node else my_num
    name, key, chash = resolve_channel(iface, args.channel_index)
    pubkey = None
    if args.public_key:
        import base64

        pubkey = base64.b64decode(args.public_key)
    print(
        f"target=0x{my_num:08x} channel[{args.channel_index}]='{name}' hash={chash} keylen={len(key)}"
    )

    encrypt = not args.no_encrypt

    def inject_payload(portnum, payload, want_response=False):
        pid = int(args.pid, 0) if args.pid else rand_id()
        if encrypt:
            if not key:
                sys.exit("channel has no key; use --no-encrypt or a keyed channel")
            inner = aes_ctr(key, from_node, pid, build_data(portnum, payload, want_response))
        else:
            inner = payload  # decoded path carries the raw app payload
        send_frame(
            iface,
            from_node=from_node,
            to_node=to_node,
            packet_id=pid,
            ch_hash=chash,
            inner_portnum=portnum,
            inner_bytes=inner,
            encrypted=encrypt,
            pki_encrypted=args.pki,
            public_key=pubkey or b"",
            want_ack=args.want_response,
        )

    if args.cmd == "text":
        inject_payload(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP, args.body.encode(), args.want_response
        )
    elif args.cmd == "raw":
        inject_payload(args.portnum, bytes.fromhex(args.payload_hex), args.want_response)
    elif args.cmd == "admin":
        am = admin_pb2.AdminMessage()
        am.set_owner.long_name = args.long
        am.set_owner.short_name = args.short
        if args.session_hex:
            am.session_passkey = bytes.fromhex(args.session_hex)
        inject_payload(portnums_pb2.PortNum.ADMIN_APP, am.SerializeToString(), True)
    elif args.cmd == "ciphertext":
        pid = int(args.pid, 0) if args.pid else rand_id()
        send_frame(
            iface,
            from_node=from_node,
            to_node=to_node,
            packet_id=pid,
            ch_hash=chash,
            inner_portnum=UNKNOWN_APP,
            inner_bytes=bytes.fromhex(args.hex),
            encrypted=True,
            pki_encrypted=args.pki,
            public_key=pubkey or b"",
        )
    elif args.cmd == "fuzz":
        rng = random.Random(args.seed)
        for i in range(args.n):
            blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 240)))
            pid = rand_id()
            send_frame(
                iface,
                from_node=from_node or 0x1000 + i,
                to_node=to_node,
                packet_id=pid,
                ch_hash=rng.randint(0, 255),
                inner_portnum=UNKNOWN_APP,
                inner_bytes=blob,
                encrypted=True,
            )
            time.sleep(0.05)

    time.sleep(1.5)
    iface.close()


if __name__ == "__main__":
    main()
