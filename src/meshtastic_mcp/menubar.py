# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""macOS menu-bar controller for the FleetSuite service.

A tiny status-bar app (``rumps``) that shows whether the launchd-managed
FleetSuite service is up (🟢 running · 🟡 starting · 🔴 stopped) and lets you
start / stop / restart it and open the web UI — one click each.

Optional and macOS-only (the ``[menubar]`` extra). The service runs perfectly
well without it. Launch by hand with ``meshtastic-mcp-menubar``, or install it
as a login LaunchAgent with ``scripts/install-menubar.sh``.

The controller keeps no state of its own: it drives the very same launchd agent
the service is managed by (``com.meshtastic.fleetsuite``). "Stop" means *bootout*
(unload the agent) rather than a kill — the agent's ``KeepAlive`` would respawn a
plain kill immediately, so a kill could never actually stop the service.
"""

from __future__ import annotations

import os
import subprocess
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

LABEL = "com.meshtastic.fleetsuite"
UI_URL = "http://127.0.0.1:8765"
HEALTH_URL = f"{UI_URL}/api/nightly"
POLL_SECONDS = 5.0
_LAUNCHCTL_TIMEOUT = 10.0
_HEALTH_TIMEOUT = 2.0

# The menu-bar title *is* the icon — a status glyph, no image asset required.
GLYPH_RUNNING = "🟢"
GLYPH_PENDING = "🟡"
GLYPH_STOPPED = "🔴"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_domain()}/{LABEL}"


def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``launchctl`` and never raise — a control click must not crash the
    app if launchd is momentarily uncooperative; the next poll re-reads truth."""
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))


def parse_service_state(print_output: str) -> tuple[bool, int | None]:
    """Extract ``(running, pid)`` from ``launchctl print`` output.

    ``running`` is True only when the job reports ``state = running``. ``pid`` is
    the live PID when the job has one (absent while merely loaded/scheduled).
    Pure — unit-tested directly, no launchd required.
    """
    running = False
    pid: int | None = None
    for raw in print_output.splitlines():
        line = raw.strip()
        if line.startswith("state ="):
            running = "running" in line
        elif line.startswith("pid ="):
            value = line.split("=", 1)[1].strip()
            pid = int(value) if value.isdigit() else None
    return running, pid


def service_pid() -> int | None:
    """The FleetSuite service PID if it is loaded *and* running, else None."""
    result = _launchctl("print", _service_target())
    if result.returncode != 0:  # not bootstrapped at all
        return None
    running, pid = parse_service_state(result.stdout)
    return pid if running else None


def http_healthy() -> bool:
    """True when the service answers on :8765 (running vs. still starting up)."""
    try:
        with urlopen(HEALTH_URL, timeout=_HEALTH_TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except (URLError, OSError, ValueError):
        return False


def status_line() -> tuple[str, str]:
    """(glyph, human status) for the current service state."""
    pid = service_pid()
    if pid is None:
        return GLYPH_STOPPED, "FleetSuite: stopped"
    if http_healthy():
        return GLYPH_RUNNING, f"FleetSuite: running · pid {pid}"
    return GLYPH_PENDING, f"FleetSuite: starting… · pid {pid}"


def start_service() -> None:
    """Load the agent (``RunAtLoad`` then starts it); kickstart covers the case
    where it was already loaded but idle. Both are no-ops if already running."""
    _launchctl("bootstrap", _domain(), _plist_path())
    _launchctl("kickstart", _service_target())


def stop_service() -> None:
    """Unload the agent. Bootout — not kill — because ``KeepAlive`` respawns a
    killed job; unloading is the only thing that actually keeps it down."""
    _launchctl("bootout", _domain(), _plist_path())


def restart_service() -> None:
    """Kill-and-restart the running job (``KeepAlive`` keeps it, so -k cycles)."""
    _launchctl("kickstart", "-k", _service_target())


def open_ui() -> None:
    webbrowser.open(UI_URL)


def main() -> None:
    # rumps is imported lazily so this module stays importable (and unit-testable)
    # on non-macOS / without the [menubar] extra installed.
    import rumps

    app = rumps.App("FleetSuite", title=GLYPH_PENDING, quit_button=None)
    status = rumps.MenuItem("Checking…")

    def refresh(_sender: object | None = None) -> None:
        app.title, status.title = status_line()

    def act(action):
        def _callback(_sender: object) -> None:
            action()
            refresh()

        return _callback

    app.menu = [
        status,
        None,
        rumps.MenuItem("Open FleetSuite…", callback=lambda _s: open_ui()),
        None,
        rumps.MenuItem("Start", callback=act(start_service)),
        rumps.MenuItem("Stop", callback=act(stop_service)),
        rumps.MenuItem("Restart", callback=act(restart_service)),
        None,
        rumps.MenuItem("Quit Menu Bar App", callback=rumps.quit_application),
    ]

    refresh()
    rumps.Timer(refresh, POLL_SECONDS).start()
    app.run()


if __name__ == "__main__":  # pragma: no cover
    main()
