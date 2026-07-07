# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Bridge-semantics: the CoT an app's TAK server would forward to ATAK/iTAK.

Meshtastic apps (≥2.8) run an in-app local TAK server that bridges mesh TAK
traffic to a connected ATAK/iTAK client: it decompresses the TAKPacketV2 wire
(portnum 78) and rebuilds a Cursor-on-Target (CoT) XML event over the TAK
stream. These tests take the *exact* v2 payloads our sim emits and run them
through the SDK's decompress → CoT-XML build path — i.e. what the bridge does —
asserting the resulting CoT is well-formed and carries the right identity,
type, position, and chat. This validates our TAK stimulus end-to-end without an
emulator; the full app-plane loop lives in ``scripts/ci_atak_app_loop.py``.

SDK-gated: skips when the ``[tak]`` extra (meshtastic-tak) isn't installed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp.replay import metrics, sim, tak

requires_tak = pytest.mark.skipif(not tak.available(), reason="[tak] extra not installed")

TAK_V2_PORT = 78


def _v2_payloads(cap):
    for _t, raw, _ch in cap.packets:
        mp = mesh_pb2.MeshPacket()
        mp.ParseFromString(raw)
        if mp.WhichOneof("payload_variant") == "decoded" and mp.decoded.portnum == TAK_V2_PORT:
            yield mp.decoded.payload


def _bridge_to_cot(wire: bytes) -> str:
    """Reproduce the app TAK server's receive path: wire → TAKPacketV2 → CoT XML."""
    from meshtastic_tak import CotXmlBuilder, TakCompressor

    pkt = TakCompressor().decompress(wire)
    return CotXmlBuilder().build(pkt)


@requires_tak
def test_wrapper_pli_bridges_to_valid_typed_cot():
    pli = tak.build_pli(
        callsign="Coyote-1",
        uid="MESH-deadbeef",
        team=10,
        role=1,
        lat_i=407_864_000,
        lon_i=-1_192_065_000,
        altitude=1200,
        speed=3,
        course=90,
        battery=88,
    )
    cot = _bridge_to_cot(tak.compress(pli))
    root = ET.fromstring(cot)  # well-formed XML
    assert root.tag == "event"
    assert root.attrib["uid"] == "MESH-deadbeef"
    assert root.attrib["type"] == tak.PLI_COT_TYPE  # typed, not empty
    point = root.find("point")
    assert point is not None
    assert abs(float(point.attrib["lat"]) - 40.7864) < 1e-4
    assert abs(float(point.attrib["lon"]) - -119.2065) < 1e-4
    contact = root.find("detail/contact")
    assert contact is not None and contact.attrib["callsign"] == "Coyote-1"


@requires_tak
def test_wrapper_chat_bridges_to_geochat_remarks():
    chat = tak.build_chat(
        callsign="Sage-2",
        uid="MESH-c0ffee",
        team=9,
        role=2,
        battery=70,
        message="rally at CP1",
    )
    cot = _bridge_to_cot(tak.compress(chat))
    root = ET.fromstring(cot)
    assert root.attrib["uid"] == "MESH-c0ffee"
    remarks = root.find("detail/remarks")
    assert remarks is not None and remarks.text == "rally at CP1"


@requires_tak
def test_sim_v2_squad_bridges_to_renderable_cot():
    """Every v2 payload the sim streams for a TAK squad bridges to a CoT event
    ATAK/iTAK would render: valid XML, MESH- uid, and a friendly-ground type on
    the PLIs. This is the payload-level guarantee behind the app-plane loop."""
    prof = {"tak": {"team_nodes": 4, "pli_interval": 60, "chat_per_hour": 2, "wire": "v2"}}
    cap = sim.generate(nodes=150, days=1, seed=8, start=1_700_000_000, profile=prof)
    assert metrics.capture_stats(cap)["tak_packets"] > 0

    n_pli = n_chat = 0
    callsigns: set[str] = set()
    for payload in _v2_payloads(cap):
        cot = _bridge_to_cot(payload)
        root = ET.fromstring(cot)  # must be well-formed
        assert root.tag == "event"
        assert root.attrib["uid"].startswith("MESH-")
        contact = root.find("detail/contact")
        assert contact is not None
        callsigns.add(contact.attrib.get("callsign", ""))
        if root.find("detail/remarks") is not None:
            n_chat += 1
        else:
            assert root.attrib["type"] == tak.PLI_COT_TYPE
            point = root.find("point")
            assert point is not None and point.attrib["lat"] and point.attrib["lon"]
            n_pli += 1
    assert n_pli > 0 and n_chat > 0
    assert len(callsigns) == 4  # one CoT identity per squad member


# A realistic ATAK-authored CoT event (what ATAK-CIV puts on its TAK stream when
# a user drops a self-PLI / sends chat), including the detail bloat the mesh strips.
_ATAK_PLI = (
    '<?xml version="1.0"?><event version="2.0" uid="ANDROID-r1" type="a-f-G-U-C" '
    'how="m-g" time="2026-01-01T00:00:00Z" start="2026-01-01T00:00:00Z" '
    'stale="2026-01-01T00:05:00Z"><point lat="40.79" lon="-119.21" hae="1200" '
    'ce="9.9" le="9.9"/><detail><contact callsign="RANGER-1"/>'
    '<__group name="Cyan" role="Team Member"/>'
    '<takv device="Pixel" platform="ATAK-CIV" version="5.5"/>'
    '<status battery="77"/></detail></event>'
)
_ATAK_GEOCHAT = (
    '<?xml version="1.0"?><event version="2.0" uid="GeoChat.ANDROID-r1.All Chat Rooms.x" '
    'type="b-t-f" how="m-g" time="2026-01-01T00:00:00Z" start="2026-01-01T00:00:00Z" '
    'stale="2026-01-01T00:05:00Z"><point lat="40.79" lon="-119.21" hae="1200" ce="9.9" '
    'le="9.9"/><detail><contact callsign="RANGER-1"/><remarks>moving to OP</remarks>'
    '<__chat chatroom="All Chat Rooms" senderCallsign="RANGER-1"/></detail></event>'
)


@requires_tak
def test_atak_cot_converts_to_mesh_wire_send_leg():
    """Send leg (TAK client -> mesh): an ATAK-authored CoT becomes a valid
    portnum-78 mesh payload that decompresses back to the same identity/pos."""
    wire = tak.cot_to_wire(_ATAK_PLI)
    assert 0 < len(wire) <= 184  # LoRa-sized after mesh stripping
    pkt = tak.decompress(wire)
    assert pkt.uid == "ANDROID-r1"
    assert pkt.callsign == "RANGER-1"
    assert abs(pkt.latitude_i - 407_900_000) <= 1
    assert abs(pkt.longitude_i - -1_192_100_000) <= 1
    # and it rides ATAK_PLUGIN_V2 (78) on the wire
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", 0x1234)
    mp.decoded.portnum = 78
    mp.decoded.payload = wire
    assert mp.decoded.portnum == 78


@requires_tak
def test_atak_geochat_converts_to_mesh_wire():
    pkt = tak.parse_cot(_ATAK_GEOCHAT)
    assert pkt.chat.message == "moving to OP"
    assert tak.compress(pkt)  # compresses without error


@requires_tak
def test_cot_round_trips_back_through_parser():
    """The bridge is reversible: the CoT XML we emit parses back to an
    equivalent TAKPacketV2 (what an inbound ATAK→mesh path would do)."""
    from meshtastic_tak import CotXmlParser

    pli = tak.build_pli(
        callsign="Mesa-3",
        uid="MESH-1234abcd",
        team=10,
        role=1,
        lat_i=360_000_000,
        lon_i=-1_150_000_000,
        altitude=800,
        speed=0,
        course=0,
        battery=55,
    )
    cot = _bridge_to_cot(tak.compress(pli))
    back = CotXmlParser().parse(cot)
    assert back.uid == "MESH-1234abcd"
    assert back.callsign == "Mesa-3"
