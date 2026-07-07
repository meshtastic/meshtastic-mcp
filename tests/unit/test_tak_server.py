# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CoT streaming TAK server — the app-plane stimulus a TAK client connects to.

Exercises the same server the Android ATAK e2e loop drives, but with a local
socket client instead of ATAK: it asserts the sim's TAK squad is delivered as
well-formed, live CoT over the TAK TCP stream. SDK-gated ([tak] extra).
"""

from __future__ import annotations

import socket
import time
import xml.etree.ElementTree as ET

import pytest

from meshtastic_mcp.replay import sim, tak, tak_server

requires_tak = pytest.mark.skipif(not tak.available(), reason="[tak] extra not installed")


def _squad_capture():
    prof = {"tak": {"team_nodes": 4, "pli_interval": 60, "chat_per_hour": 2, "wire": "v2"}}
    return sim.generate(nodes=120, days=1, seed=8, start=1_700_000_000, profile=prof)


@requires_tak
def test_capture_to_cot_events_are_wellformed():
    events = tak_server.capture_to_cot_events(_squad_capture())
    assert events
    # time-ordered, each a bare CoT <event> (no XML declaration)
    times = [t for t, _ in events]
    assert times == sorted(times)
    callsigns = set()
    for _t, cot in events:
        assert not cot.lstrip().startswith(b"<?xml")
        root = ET.fromstring(cot)
        assert root.tag == "event"
        assert root.attrib["uid"].startswith("MESH-")
        contact = root.find("detail/contact")
        assert contact is not None
        callsigns.add(contact.attrib.get("callsign", ""))
    assert len(callsigns) == 4


@requires_tak
def test_server_streams_live_cot_to_a_client():
    events = tak_server.capture_to_cot_events(_squad_capture())
    # fast + looped so the client reliably gets several events quickly
    srv = tak_server.CotTakServer(events, host="127.0.0.1", port=0, speed=100_000.0, loop=True)
    port = srv.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.settimeout(5)
            buf = b""
            deadline = time.time() + 5
            while b"</event>" not in buf and time.time() < deadline:
                buf += c.recv(4096)
        assert b"<event" in buf and b"</event>" in buf
        # parse the first complete event and check it's live + typed
        first = buf[buf.index(b"<event") : buf.index(b"</event>") + len(b"</event>")]
        root = ET.fromstring(first)
        assert root.attrib["uid"].startswith("MESH-")
        # restamped to "now" (year matches current UTC), not the 2023 capture epoch
        assert root.attrib["time"].startswith(time.strftime("%Y", time.gmtime()))
    finally:
        srv.stop()
    assert srv.clients_served >= 1 and srv.events_sent >= 1


@requires_tak
def test_server_is_bidirectional_receives_client_cot():
    """Send leg (TAK client -> mesh): a client-authored CoT event sent to the
    server is captured and converts to a mesh TAKPacketV2."""
    from meshtastic_mcp.replay import tak

    events = tak_server.capture_to_cot_events(_squad_capture())
    srv = tak_server.CotTakServer(events, host="127.0.0.1", port=0, speed=100_000.0, loop=True)
    port = srv.start()
    client_cot = (
        '<event version="2.0" uid="ANDROID-r1" type="a-f-G-U-C" how="m-g" '
        'time="2026-01-01T00:00:00Z" start="2026-01-01T00:00:00Z" '
        'stale="2026-01-01T00:05:00Z"><point lat="40.79" lon="-119.21" hae="1200" '
        'ce="9.9" le="9.9"/><detail><contact callsign="RANGER-1"/>'
        '<__group name="Cyan" role="Team Member"/></detail></event>'
    )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.settimeout(5)
            c.recv(4096)  # let the receive leg stream at least once
            c.sendall(client_cot.encode())
            deadline = time.time() + 5
            while not srv.received_cot and time.time() < deadline:
                time.sleep(0.1)
        assert srv.received_cot, "server should capture the client's CoT"
        pkt = tak.parse_cot(srv.received_cot[0].decode())
        assert pkt.uid == "ANDROID-r1" and pkt.callsign == "RANGER-1"
        assert tak.compress(pkt)  # converts to a mesh payload
    finally:
        srv.stop()


@requires_tak
def test_server_context_manager_and_no_v2_is_empty():
    # a v1 (legacy) squad yields no CoT-v2 events (server needs wire="v2")
    v1 = sim.generate(
        nodes=60, days=1, seed=2, start=1_700_000_000, profile={"tak": {"team_nodes": 3}}
    )
    assert tak_server.capture_to_cot_events(v1) == []


def test_capture_to_cot_events_requires_extra(monkeypatch):
    # without the SDK the bridge path fails fast with the install hint
    monkeypatch.setattr(tak, "available", lambda: False)
    with pytest.raises(RuntimeError, match=r"\[tak\] extra"):
        tak_server.capture_to_cot_events(sim.generate(nodes=10, days=1, seed=1))
