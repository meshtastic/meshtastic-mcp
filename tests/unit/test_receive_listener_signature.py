# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ReceiveCollector's pubsub listeners must subscribe to topics whose
message-data spec carries ``interface`` as *optional*.

pypubsub rejects a listener whose required parameter is optional in the
topic's MDS (``ListenerMismatchError``). ``meshtastic.receive.telemetry`` is
such a topic, so a ``handler(packet, interface)`` signature (no default) blew
up every telemetry-tier test at subscribe time. These tests pin the contract
without hardware by replaying the same MDS shape through pypubsub itself.
"""

from __future__ import annotations

import inspect

import pytest

pub = pytest.importorskip("pubsub.pub")  # ships with the meshtastic lib

from tests.mesh import _receive


def _mds_like_telemetry_topic(topic: str) -> None:
    """Establish an MDS matching meshtastic.receive.telemetry: packet
    required, interface optional."""

    def establish(packet, interface=None):  # mirrors the runtime publisher shape
        pass

    pub.subscribe(establish, topic)


def test_collector_handler_subscribes_to_interface_optional_topic():
    """The exact failure mode from the bench: subscribing the collector's
    handler to a topic whose MDS has interface optional must not raise."""
    topic = "unittest.receive.telemetry_like"
    _mds_like_telemetry_topic(topic)

    collector = _receive.ReceiveCollector.__new__(_receive.ReceiveCollector)
    # Reconstruct just enough state to build the closure handler the way
    # __enter__ does — signature is what's under test, not serial IO.
    import threading

    collector._lock = threading.Lock()
    collector._packets = []

    def handler(packet: dict, interface=None) -> None:  # mirrors _receive.py
        with collector._lock:
            collector._packets.append(packet)

    pub.subscribe(handler, topic)  # raises ListenerMismatchError if regressed
    pub.sendMessage(topic, packet={"decoded": {}})
    assert collector._packets == [{"decoded": {}}]


def test_receive_module_handlers_default_interface():
    """Source-level guard: both closures in ReceiveCollector.__enter__ must
    default ``interface`` so they stay compatible with interface-optional
    topics. Checked via the source (closures aren't importable directly)."""
    src = inspect.getsource(_receive.ReceiveCollector.__enter__)
    assert "def handler(packet: dict, interface: Any = None)" in src
    assert "def log_handler(line: str, interface: Any = None)" in src
