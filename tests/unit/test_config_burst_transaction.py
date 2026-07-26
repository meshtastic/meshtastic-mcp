"""Config-burst behaviour against a real firmware AdminModule, with no radio attached.

Runs `meshtasticd` (real firmware compiled for the host) as a virtual radio over TCP and replays what
a client does when it applies a device profile. Three trials, each on a freshly-erased node:

  untransacted, unpaced   -> the reboot storm; writes silently lost
  transacted, unpaced     -> WORSE: begin_edit_settings itself is dropped, so the run degrades to
                             untransacted while the client believes it is transacted
  transacted, paced       -> one deferred save, one reboot, every write lands

Why this matters: the firmware acks ToRadio writes it discards. A client cannot tell during a run
whether config landed, so a regression here is invisible until someone's radio comes back
half-configured. Observed on a Heltec V4 (ESP32-S3) and reproduced here without hardware.

Skipped unless a meshtasticd binary is available:
    MESHTASTIC_MESHTASTICD_BIN=/path/to/meshtasticd  (or scripts/build_meshtasticd.sh --dest ...)
"""

from __future__ import annotations

import os
import re
import socket
import time
from pathlib import Path

import pytest

pytest.importorskip("meshtastic.tcp_interface")

from meshtastic_mcp.emulator import native_node

BINARY_ENV = "MESHTASTIC_MESHTASTICD_BIN"
_LOG_PATTERNS = {
    "set_module": re.compile(r"Set module config"),
    "save": re.compile(r"Save changes to disk"),
    "deferred": re.compile(r"Delay save of changes to disk"),
    "reboot": re.compile(r"Reboot in \d+ seconds"),
    "begin": re.compile(r"Begin transaction for editing settings"),
    "commit": re.compile(r"Commit transaction for edited settings"),
}

# Eight module-config sections. Every one of them reboots when written outside a transaction
# (handleSetModuleConfig defaults shouldReboot = true), which is the behaviour under test.
_SECTIONS = [
    ("telemetry", "device_update_interval"),
    ("neighbor_info", "update_interval"),
    ("paxcounter", "paxcounter_update_interval"),
    ("detection_sensor", "minimum_broadcast_secs"),
    ("store_forward", "history_return_max"),
    ("range_test", "sender"),
    ("ambient_lighting", "current"),
    ("telemetry", "environment_update_interval"),
]


def _binary() -> Path:
    raw = os.environ.get(BINARY_ENV, "")
    if not raw:
        pytest.skip(f"set {BINARY_ENV} to a meshtasticd binary to run this")
    path = Path(raw)
    if not path.is_file():
        pytest.skip(f"{BINARY_ENV}={raw} is not a file")
    return path


def _wait_tcp(port: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(1)
    return False


def _counts(log: str) -> dict[str, int]:
    lines = log.splitlines()
    return {
        name: sum(1 for line in lines if pat.search(line)) for name, pat in _LOG_PATTERNS.items()
    }


def _trial(tmp_path: Path, port: int, *, transacted: bool, pace: float) -> dict[str, int]:
    """Apply the burst to a freshly-erased node and return its log signature."""
    import meshtastic.tcp_interface

    node = native_node.build_lab(
        binary=_binary(), workdir=tmp_path / f"node-{port}", count=1, base_port=port
    )[0]
    node.start(erase=True)
    try:
        if not _wait_tcp(node.tcp_port):
            pytest.skip("meshtasticd did not open its TCP API in time")
        iface = meshtastic.tcp_interface.TCPInterface(
            hostname="127.0.0.1", portNumber=node.tcp_port
        )
        time.sleep(3)  # let the initial config download settle
        local = iface.localNode
        for section, field in _SECTIONS:
            holder = getattr(local.moduleConfig, section)
            current = getattr(holder, field)
            setattr(holder, field, current + 1 if current < 3000 else current - 1)

        if transacted:
            local.beginSettingsTransaction()
        for section, _field in _SECTIONS:
            local.writeConfig(section)
            if pace:
                time.sleep(pace)
        if transacted:
            local.commitSettingsTransaction()

        time.sleep(9)  # outlast the 7s reboot timer so the log shows what happened
        try:
            iface.close()
        except Exception:
            pass
        return _counts(Path(node.log_path).read_text(errors="replace"))
    finally:
        try:
            node.stop()
        except Exception:
            pass


@pytest.mark.meshtasticd
def test_untransacted_burst_causes_a_reboot_per_write(tmp_path: Path) -> None:
    counts = _trial(tmp_path, 4430, transacted=False, pace=0.0)
    # Every accepted write saves to flash and schedules its own reboot.
    assert counts["save"] >= 2
    assert counts["reboot"] == counts["save"]
    assert counts["deferred"] == 0
    # And the radio cannot keep up, so some writes never arrive at all. This is the silent loss:
    # the client is acked for every one of them.
    assert counts["set_module"] < len(_SECTIONS)


@pytest.mark.meshtasticd
def test_paced_transaction_collapses_to_one_save_and_one_reboot(tmp_path: Path) -> None:
    counts = _trial(tmp_path, 4431, transacted=True, pace=0.15)
    assert counts["begin"] == 1
    assert counts["commit"] == 1
    assert counts["set_module"] == len(_SECTIONS)  # nothing dropped
    assert counts["deferred"] == len(_SECTIONS)  # every save deferred to the commit
    assert counts["save"] == 1
    assert counts["reboot"] == 1


@pytest.mark.meshtasticd
def test_unpaced_transaction_can_lose_begin_and_silently_untransact(tmp_path: Path) -> None:
    """Pacing is load-bearing, not cosmetic.

    Without it the opening begin_edit_settings can be dropped like any other write. The firmware
    never acks it, so the client cannot tell, and the whole run degrades to untransacted: a save and
    a reboot per write. That is strictly worse than not using a transaction, because the client
    reports success. Asserted as a property rather than an exact count because it is a race.
    """
    counts = _trial(tmp_path, 4432, transacted=True, pace=0.0)
    if counts["begin"] == 0:
        # The degraded path: saves were NOT deferred, so the reboot storm happened anyway.
        assert counts["deferred"] == 0
        assert counts["save"] >= 1
    else:
        # It got through this time; the transaction must then have behaved.
        assert counts["deferred"] >= 1
