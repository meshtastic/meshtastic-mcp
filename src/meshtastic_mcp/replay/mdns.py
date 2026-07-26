# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""mDNS/Bonjour advertisement for replay sessions.

Real Meshtastic firmware advertises its TCP API as ``_meshtastic._tcp`` on
``local.`` with TXT records the apps use to render the discovery row: the
Apple app's ``TCPTransport`` shows ``<shortname>_<last 4 of id>`` (e.g.
``RPLY_4331``) built from the ``shortname`` and ``id`` TXT keys, falling back
to ``<instance> (<ip>)`` when TXT is absent. Advertising the replay session the
same way makes it appear in an app's device list like any real node — no
manual IP entry.

Best-effort by design (an advertisement failure must never break a session)
and dependency-light per the repo rules: prefers the pure-Python ``zeroconf``
package when importable, else shells out to ``dns-sd`` (ships with macOS) or
``avahi-publish-service`` (Linux, avahi-utils), else stays silent and reports
an actionable hint in ``replay_status``.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import sys
from typing import Any

SERVICE_TYPE = "_meshtastic._tcp"

_HINT = "no mDNS backend: pip install zeroconf, or install dns-sd (macOS) / avahi-utils (Linux)"


def txt_records(shortname: str, node_id: str) -> dict[str, str]:
    """The TXT keys apps read: shortname + id (display: shortname_<id[-4:]>)."""
    return {"shortname": shortname, "id": node_id}


def dnssd_argv(instance: str, port: int, txt: dict[str, str]) -> list[str]:
    """`dns-sd -R` registration argv (blocks while registered; kill to expire)."""
    return [
        "dns-sd",
        "-R",
        instance,
        SERVICE_TYPE,
        "local.",
        str(port),
        *[f"{k}={v}" for k, v in txt.items()],
    ]


def avahi_argv(instance: str, port: int, txt: dict[str, str]) -> list[str]:
    """`avahi-publish-service` argv (blocks while registered; kill to expire)."""
    return [
        "avahi-publish-service",
        instance,
        SERVICE_TYPE,
        str(port),
        *[f"{k}={v}" for k, v in txt.items()],
    ]


class Advertiser:
    """Advertise one replay session; ``start()``/``stop()`` bracket its lifetime."""

    def __init__(self, instance: str, port: int, txt: dict[str, str]):
        # dots inside an instance label read as label separators to resolvers
        self.instance = instance.replace(".", "-")
        self.port = port
        self.txt = txt
        self.backend: str | None = None
        self.error: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._zc: Any = None
        self._zc_info: Any = None

    # -- backends, in preference order --
    def _try_zeroconf(self) -> bool:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return False
        from .engine import local_ips

        addrs = [socket.inet_aton(ip) for ip in local_ips() if not ip.startswith("127.")]
        if not addrs:
            return False
        info = ServiceInfo(
            f"{SERVICE_TYPE}.local.",
            f"{self.instance}.{SERVICE_TYPE}.local.",
            addresses=addrs,
            port=self.port,
            properties=self.txt,
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        self._zc, self._zc_info = zc, info
        self.backend = "zeroconf"
        return True

    def _try_subprocess(self, argv: list[str]) -> bool:
        if shutil.which(argv[0]) is None:
            return False
        # The registration lives as long as the process; stop() terminates it,
        # which also sends the mDNS goodbye so browsers drop the row promptly.
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # These helpers stay resident while registered; an immediate exit means a
        # name conflict or no running daemon — don't claim we advertised.
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=0.3)
            self._proc = None
            return False
        self.backend = argv[0]
        return True

    def start(self) -> Advertiser:
        try:
            if self._try_zeroconf():
                return self
            if sys.platform == "darwin" and self._try_subprocess(
                dnssd_argv(self.instance, self.port, self.txt)
            ):
                return self
            if self._try_subprocess(avahi_argv(self.instance, self.port, self.txt)):
                return self
            self.error = _HINT
        except Exception as exc:  # advertisement must never break the session
            self.error = str(exc)
        return self

    def stop(self) -> None:
        if self._zc is not None:
            try:
                self._zc.unregister_service(self._zc_info)
                self._zc.close()
            except Exception:
                pass
            self._zc = self._zc_info = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None
        self.backend = None

    def status(self) -> dict[str, Any]:
        display = f"{self.txt.get('shortname', '')}_{self.txt.get('id', '')[-4:]}"
        return {
            "advertised": self.backend is not None,
            "backend": self.backend,
            "instance": self.instance,
            "display_name": display if self.backend else None,
            "error": self.error,
        }
