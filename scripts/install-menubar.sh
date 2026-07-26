#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

# Install the FleetSuite menu-bar controller as a macOS login LaunchAgent, so a
# 🟢/🟡/🔴 status item with start/stop/restart sits in your menu bar and returns
# after logout/reboot. Companion to install-launchd.sh (the service itself).
#
#   ./scripts/install-menubar.sh              # install + (re)start
#   ./scripts/install-menubar.sh --uninstall  # stop + remove
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.meshtastic.fleetsuite.menubar"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
	launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || true
	rm -f "$DEST"
	echo "removed $DEST"
	exit 0
fi

if [[ "$(uname)" != "Darwin" ]]; then
	echo "the menu-bar controller is macOS-only" >&2
	exit 1
fi

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
	echo "no venv at $ROOT/.venv — run scripts/fleetsuite.sh once first" >&2
	exit 1
fi

# Ensure the entry point + rumps are present in the venv.
if [[ ! -x "$ROOT/.venv/bin/meshtastic-mcp-menubar" ]]; then
	echo "installing the [menubar] extra (rumps)…"
	"$PY" -m pip install --quiet -e "$ROOT[menubar]"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

sed -e "s|@ROOT@|$ROOT|g" -e "s|@HOME@|$HOME|g" \
	"$ROOT/scripts/com.meshtastic.fleetsuite.menubar.plist" >"$DEST"

# Re-bootstrap cleanly whether or not it was already loaded.
launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DEST"

echo "installed: $DEST"
echo "logs:      $HOME/Library/Logs/fleetsuite-menubar.log"
echo "A 🟢/🟡/🔴 'FleetSuite' item should appear in your menu bar."
