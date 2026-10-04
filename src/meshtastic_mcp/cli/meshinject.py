#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""
meshinject - inject packets into a locally-connected Meshtastic board as if they arrived off the LoRa
radio. Requires the target to run firmware built with -D MESHTASTIC_ENABLE_FRAME_INJECTION=1 (portduino
sim nodes support it unconditionally).

A crafted frame rides inside an InjectedFrame envelope wrapped in a MeshPacket sent on the SIMULATOR_APP
portnum (68). The firmware (MeshService::injectAsReceived) unwraps it and delivers it through the real
receive pipeline, so it gets from!=0 enforcement, channel/PKC decryption, hop handling, dedup, and
module dispatch - exactly like an over-the-air packet.

  InjectedFrame.portnum == UNKNOWN_APP -> InjectedFrame.data is verbatim CIPHERTEXT (firmware decrypts it)
  InjectedFrame.portnum == <portnum>   -> InjectedFrame.data is the DECODED payload for that portnum

3.0 channel encryption (AES-CCM over the frame header) is broadcast only; a frame to one node is PKI.
The framing and crypto are meshtastic_mcp.inject's.

Run with the meshtastic-CLI venv python (has meshtastic + cryptography).
"""

import argparse
import random
import sys
import time

import meshtastic.serial_interface as ser
import meshtastic.tcp_interface as tcp
from meshtastic import admin_pb2, portnums_pb2

from meshtastic_mcp.inject import (
    BROADCAST,
    UNKNOWN_APP,
    _build_data,
    _header,
    _rand_id,
    _resolve_channel,
    _send,
    channel_encrypt,
)


def connect(args):
    if args.serial:
        return ser.SerialInterface(devPath=args.serial)
    host, _, port = (args.host or "localhost").partition(":")
    return tcp.TCPInterface(host, portNumber=int(port) if port else 4403)


def send_frame(iface, mp, *, inner_portnum, inner_bytes, encrypted):
    """Inject via SIMULATOR_APP and print what went out."""
    sent = _send(
        iface, mp, inner_portnum=inner_portnum, inner_bytes=inner_bytes, encrypted=encrypted
    )
    kind = "encrypted" if encrypted else "decoded"
    print(
        f"injected {kind} frame: from={sent['from']} to={sent['to']} id={sent['id']} "
        f"ch={sent['channel_hash']} portnum={inner_portnum} len={len(inner_bytes)}"
        f"{' pki' if sent['pki_encrypted'] else ''}"
    )


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
        help="destination NodeNum (default: broadcast for a channel frame, else the target's own num)",
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
    encrypt = not args.no_encrypt
    channel_frame = encrypt and not args.pki and args.cmd in ("text", "raw", "admin")
    if args.to_node:
        to_node = int(args.to_node, 0)
    else:
        to_node = BROADCAST if channel_frame else my_num
    if channel_frame and to_node != BROADCAST:
        sys.exit(
            "3.0 channel encryption is broadcast only; use --pki --no-encrypt for one node, or drop --to"
        )
    try:
        name, key, chash = _resolve_channel(iface, args.channel_index)
    except ValueError as e:
        sys.exit(str(e))
    pubkey = None
    if args.public_key:
        import base64

        pubkey = base64.b64decode(args.public_key)
    print(
        f"target=0x{my_num:08x} channel[{args.channel_index}]='{name}' hash={chash} keylen={len(key)}"
    )

    def header(pid=None, **kw):
        return _header(
            from_node=kw.pop("from_node", from_node),
            to_node=to_node,
            packet_id=pid if pid is not None else (int(args.pid, 0) if args.pid else _rand_id()),
            ch_hash=kw.pop("ch_hash", chash),
            **kw,
        )

    def inject_payload(portnum, payload, want_response=False):
        mp = header(pki_encrypted=args.pki, public_key=pubkey or b"", want_ack=args.want_response)
        if encrypt:
            if not key:
                sys.exit("channel has no key; use --no-encrypt or a keyed channel")
            inner = channel_encrypt(key, mp, _build_data(portnum, payload, want_response))
        else:
            inner = payload  # decoded path carries the raw app payload
        send_frame(iface, mp, inner_portnum=portnum, inner_bytes=inner, encrypted=encrypt)

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
        mp = header(pki_encrypted=args.pki, public_key=pubkey or b"")
        send_frame(
            iface,
            mp,
            inner_portnum=UNKNOWN_APP,
            inner_bytes=bytes.fromhex(args.hex),
            encrypted=True,
        )
    elif args.cmd == "fuzz":
        rng = random.Random(args.seed)
        for i in range(args.n):
            blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 240)))
            mp = header(_rand_id(), from_node=from_node or 0x1000 + i, ch_hash=rng.randint(0, 255))
            send_frame(iface, mp, inner_portnum=UNKNOWN_APP, inner_bytes=blob, encrypted=True)
            time.sleep(0.05)

    time.sleep(1.5)
    iface.close()


if __name__ == "__main__":
    main()
