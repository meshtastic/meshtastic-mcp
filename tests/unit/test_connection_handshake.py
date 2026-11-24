# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""The config handshake: a lost `want_config` is sent again, and the wait honors the timeout."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from meshtastic.mesh_interface import MeshInterface

from meshtastic_mcp import connection


class _Iface:
    """A radio that ignores the first `lost` config requests, as one restarting does."""

    def __init__(self, lost: int, timeout: int = 8, answers: bool = True) -> None:
        self.noProto = False
        self.failure = None
        self.isConnected = threading.Event()
        self.myInfo = None
        self._timeout = SimpleNamespace(expireTimeout=timeout)
        self.requests = 0
        self._lost = lost
        self._answers = answers

    def _startConfig(self) -> None:
        self.requests += 1
        if self._answers and self.requests > self._lost:
            self.myInfo = object()
            self.isConnected.set()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(connection, "_RESEND_WANT_CONFIG_S", 0.01)


def test_a_lost_want_config_is_sent_again() -> None:
    iface = _Iface(lost=1)
    iface._startConfig()  # the library's own first request, lost
    connection._wait_connected_resending(iface)
    assert iface.requests == 2 and iface.isConnected.is_set()


def test_a_download_under_way_is_not_restarted() -> None:
    iface = _Iface(lost=99)
    iface.myInfo = object()  # the radio is answering; the rest is still coming

    def finish() -> None:
        time.sleep(0.05)
        iface.isConnected.set()

    threading.Thread(target=finish).start()
    connection._wait_connected_resending(iface)
    assert iface.requests == 0


def test_the_wait_ends_at_the_interface_timeout_not_thirty_seconds() -> None:
    iface = _Iface(lost=0, timeout=1, answers=False)
    started = time.monotonic()
    with pytest.raises(MeshInterface.MeshInterfaceError, match="Timed out"):
        connection._wait_connected_resending(iface)
    assert time.monotonic() - started < 2
    assert iface.requests > 1


def test_both_transports_use_it() -> None:
    from meshtastic.serial_interface import SerialInterface
    from meshtastic.tcp_interface import TCPInterface

    for cls in (SerialInterface, TCPInterface):
        resending = connection._resending(cls)
        assert issubclass(resending, cls)
        assert resending._waitConnected is not cls._waitConnected
