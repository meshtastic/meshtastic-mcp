#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

# Install FleetSuite as a macOS user LaunchAgent (KeepAlive + RunAtLoad), so
# it survives reboots and the nightly self-update's restart. See docs/nightly.md.
#
#   ./scripts/install-launchd.sh              # install + (re)start
#   ./scripts/install-launchd.sh --uninstall  # stop + remove
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.meshtastic.fleetsuite"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
	launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || true
	rm -f "$DEST"
	echo "removed $DEST"
	exit 0
fi

if [[ "$(uname)" != "Darwin" ]]; then
	echo "launchd is macOS-only — on Linux use a systemd user unit instead" >&2
	exit 1
fi

chmod +x "$ROOT/scripts/fleetsuite-supervisor.sh" "$ROOT/scripts/fleetsuite.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

sed -e "s|@ROOT@|$ROOT|g" -e "s|@HOME@|$HOME|g" \
	"$ROOT/scripts/com.meshtastic.fleetsuite.plist" >"$DEST"

# Re-bootstrap cleanly whether or not it was already loaded.
launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DEST"

echo "installed: $DEST"
echo "logs:      $HOME/Library/Logs/fleetsuite.log"
echo "ui:        http://127.0.0.1:8765  (Nightly tab to enable the schedule)"
