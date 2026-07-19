#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

# Single entrypoint for FleetSuite — the web UI for the Meshtastic test harness.
# Ensures deps, builds the SPA, and launches the app. One command, from anywhere.
#
#   ./scripts/fleetsuite.sh             # build SPA if needed, open the desktop window
#   ./scripts/fleetsuite.sh --browser   # serve only → http://127.0.0.1:8765
#   ./scripts/fleetsuite.sh --dev       # backend + Vite dev server with hot-reload
#   ./scripts/fleetsuite.sh --rebuild   # force a fresh SPA build first
#
# First run bootstraps the venv + web deps + npm packages automatically.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEV=0
BROWSER=0
REBUILD=0
for arg in "$@"; do
	case "$arg" in
	--dev) DEV=1 ;;
	--browser) BROWSER=1 ;;
	--rebuild) REBUILD=1 ;;
	-h | --help)
		sed -n '2,11p' "${BASH_SOURCE[0]}"
		exit 0
		;;
	*)
		echo "unknown arg: $arg (try --help)" >&2
		exit 2
		;;
	esac
done

PY="$ROOT/.venv/bin/python"
STATIC="$ROOT/src/meshtastic_mcp/web/static/index.html"
# Extra set installed into a freshly-created venv. Default is the minimal web
# backend; a bench/nightly deployment sets FLEETSUITE_EXTRAS=web,ui in its
# launchd plist so a clean redeploy gets the camera + OCR deps (soak snapshots)
# automatically. Kept opt-in because [ui] pulls opencv (and torch on non-Intel).
EXTRAS="${FLEETSUITE_EXTRAS:-web}"
# Bind address for --browser mode. Default 127.0.0.1 (localhost only) — the safe
# default: FleetSuite has NO auth and can flash/reboot/factory-reset devices. A
# deployment that wants LAN access sets FLEETSUITE_HOST=0.0.0.0 in its plist;
# only do that on a trusted network (anyone who can reach the port controls the
# fleet). The desktop-window mode always stays on 127.0.0.1.
HOST="${FLEETSUITE_HOST:-127.0.0.1}"

note() { printf '\033[36m[fleetsuite]\033[0m %s\n' "$*"; }

# 1. Python venv + extras ---------------------------------------------------
if [[ ! -x $PY ]]; then
	note "creating venv (.venv)…"
	python3 -m venv "$ROOT/.venv"
fi
if ! "$PY" -c 'import fastapi, aiosqlite, uvicorn, webview' >/dev/null 2>&1; then
	note "installing the [$EXTRAS] extra(s)…"
	"$PY" -m pip install --quiet --upgrade pip
	"$PY" -m pip install --quiet -e "$ROOT[$EXTRAS]"
fi

# 2. Dev mode: backend + Vite with HMR --------------------------------------
if [[ $DEV == 1 ]]; then
	note "dev mode (backend :8765 + Vite HMR)"
	exec "$ROOT/scripts/web-dev.sh"
fi

# 3. Frontend deps + production build ----------------------------------------
if ! command -v npm >/dev/null 2>&1; then
	echo "npm not found — install Node.js (https://nodejs.org) to build the UI." >&2
	exit 1
fi
if [[ ! -d "$ROOT/web-ui/node_modules" ]]; then
	note "installing web-ui npm packages…"
	(cd "$ROOT/web-ui" && npm install)
fi
if [[ $REBUILD == 1 || ! -f $STATIC ]]; then
	note "building the SPA…"
	(cd "$ROOT/web-ui" && npm run build)
fi

# 4. Launch ------------------------------------------------------------------
if [[ $BROWSER == 1 ]]; then
	note "serving at http://$HOST:8765 (Ctrl-C to stop)"
	exec "$ROOT/.venv/bin/meshtastic-mcp-web" --browser --host "$HOST"
fi
note "opening FleetSuite window…"
exec "$ROOT/.venv/bin/meshtastic-mcp-web"
