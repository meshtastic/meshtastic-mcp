# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Orchestrate Portduino `meshtasticd` native nodes as virtual radios over TCP/UDP.

This is the device-plane half of the hardware-free e2e lab: spin up one or more
`meshtasticd` processes (real firmware, simulated radio) that mesh with each other over
UDP multicast (``224.0.0.69:4403``) and expose a per-node TCP API the MCP server / the
Android app (emulator → ``10.0.2.2:<port>``) can connect to.

Validated 2026-06-24: two nodes mesh + deliver a text message on Linux (Docker) and on
macOS (with the framework-portduino Darwin multicast-bind fix). See the project proposal /
the ``meshtastic-e2e`` skill for the full spike write-up.

Hard-won gotchas baked in here:
- Set ``lora.region`` + ``network.enabled_protocols=UDP_BROADCAST`` via the **admin API**
  after boot — the YAML ``Region`` / ``EnableUDP`` keys do not reliably reach the protobuf
  config. Run the daemon under a restart loop so the post-write reboot relaunches it with
  persisted config.
- Multicast loopback needs a shared network namespace: one host, or one container (or
  ``--network container:<first>``), not two bridge-isolated containers.
- The official ``meshtasticd`` image's ``-h/--hwid`` flag can crash (stoi) on older tags;
  prefer YAML ``General: MACAddress`` there. The 2.8+ native build accepts ``-h``.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_NODE_YAML = """---
Lora:
  Module: sim
Config:
  EnableUDP: true
General:
  MaxNodes: 200
  MACAddress: "{mac}"
"""


@dataclass
class NativeNode:
    """A single `meshtasticd` virtual radio."""

    name: str
    tcp_port: int
    mac: str
    binary: Path
    workdir: Path
    region: str = "US"
    _proc: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def node_dir(self) -> Path:
        return self.workdir / self.name

    @property
    def config_path(self) -> Path:
        return self.workdir / f"{self.name}.yaml"

    @property
    def log_path(self) -> Path:
        return self.workdir / f"{self.name}.log"

    def _write_config(self) -> None:
        self.node_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(_NODE_YAML.format(mac=self.mac))

    def start(self, *, erase: bool = True) -> None:
        """Launch the daemon (foreground process; caller manages lifecycle)."""
        self._write_config()
        args = [
            str(self.binary),
            "-d",
            str(self.node_dir),
            "-c",
            str(self.config_path),
            "-p",
            str(self.tcp_port),
        ]
        if erase:
            args.append("-e")
        with self.log_path.open("ab") as logf:
            self._proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)

    def configure(self) -> None:
        """Set region + enable UDP multicast via the admin API, then let it reboot.

        Must run after `start()` and a short boot delay. Imports meshtastic lazily so the
        module is importable without the radio stack.
        """
        import meshtastic.tcp_interface as tcp
        from meshtastic import config_pb2

        # Explicit timeout so the library's blocking waitForConfig fails fast
        # against the acknowledged boot race instead of hanging on its internal
        # default. (This lab path intentionally skips the registry port lock —
        # it owns the node it just spawned.)
        i = tcp.TCPInterface("127.0.0.1", portNumber=self.tcp_port, timeout=15)
        try:
            n = i.localNode
            n.localConfig.lora.region = getattr(
                config_pb2.Config.LoRaConfig.RegionCode, self.region
            )
            n.localConfig.network.enabled_protocols = 1  # UDP_BROADCAST
            n.writeConfig("lora")
            time.sleep(1)
            n.writeConfig("network")
            time.sleep(2)
        finally:
            i.close()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def build_lab(
    binary: Path, workdir: Path, count: int = 2, base_port: int = 4403, region: str = "US"
) -> list[NativeNode]:
    """Construct (not yet started) N nodes with distinct ports + MACs."""
    nodes = []
    for idx in range(count):
        # Non-zero, distinct, 12-hex MAC. Must be non-zero (all-zeros reads as "blank")
        # and the YAML value is quoted in the template so it isn't parsed as an integer.
        mac = f"DE{idx:010X}"
        nodes.append(
            NativeNode(
                name=f"node{idx}",
                tcp_port=base_port + idx,
                mac=mac,
                binary=binary,
                workdir=workdir,
                region=region,
            )
        )
    return nodes
