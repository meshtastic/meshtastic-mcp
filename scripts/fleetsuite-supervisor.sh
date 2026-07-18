#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

# launchd wrapper for FleetSuite with crash-loop rollback.
#
# launchd's KeepAlive respawns FleetSuite whenever it exits — including the
# clean SIGTERM the nightly self-update sends itself. But if a self-updated
# meshtastic-mcp crashes at import, KeepAlive would respawn it forever. This
# wrapper breaks that loop: it counts consecutive short-lived starts (<60s),
# and after 3 of them rolls the checkout back to the last sha that ran
# healthily (>=120s), reinstalls, and starts that instead. The next nightly
# report surfaces the rollback as a self_update.rolled_back observation.
#
# Run via launchd (see com.meshtastic.fleetsuite.plist / install-launchd.sh);
# running it by hand works too.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.meshtastic_mcp"
FAIL_FILE="$STATE_DIR/supervisor-failures"
LAST_GOOD_FILE="$STATE_DIR/last-good-mcp-sha"

SHORT_LIVED_S=60  # an exit faster than this counts as a crash
HEALTHY_S=120     # surviving this long marks the sha as last-good
MAX_CRASHES=3     # consecutive crashes before rolling back

mkdir -p "$STATE_DIR"

note() { printf '[fleetsuite-supervisor] %s\n' "$*"; }

current_sha() { git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo ""; }

failures=0
[[ -f $FAIL_FILE ]] && failures="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"
last_good=""
[[ -f $LAST_GOOD_FILE ]] && last_good="$(cat "$LAST_GOOD_FILE" 2>/dev/null || echo "")"
sha="$(current_sha)"

if [[ $failures -ge $MAX_CRASHES && -n $last_good && -n $sha && $last_good != "$sha" ]]; then
	# NEVER hard-reset a dirty checkout — that would destroy the operator's
	# uncommitted work. Preserve it and start the current code as-is (it may
	# still crash, but that is their local change to fix, not ours to discard).
	if [[ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]]; then
		note "crash loop detected but checkout is dirty — rollback refused, starting as-is"
	else
		note "crash loop detected ($failures fast exits) — rolling back $sha -> $last_good"
		if git -C "$ROOT" reset --hard "$last_good"; then
			PY="$ROOT/.venv/bin/python"
			if [[ -x $PY ]]; then
				"$PY" -m pip install --quiet -e "${ROOT}[web]" || true
			fi
			echo 0 >"$FAIL_FILE"
			sha="$last_good"
		else
			note "rollback failed — starting the current checkout anyway"
		fi
	fi
fi

start=$(date +%s)
# Child (not exec): we must outlive it to measure its lifetime. Signals sent to
# our process group (launchd) reach the child; forward SIGTERM explicitly too.
"$ROOT/scripts/fleetsuite.sh" --browser &
child=$!
trap 'kill -TERM "$child" 2>/dev/null' TERM INT
wait "$child"
code=$?
duration=$(($(date +%s) - start))

if [[ $duration -ge $HEALTHY_S ]]; then
	if [[ -n $sha ]]; then
		echo "$sha" >"$LAST_GOOD_FILE"
	fi
	echo 0 >"$FAIL_FILE"
	note "exited after ${duration}s (code $code) — sha marked healthy"
elif [[ $duration -lt $SHORT_LIVED_S ]]; then
	echo $((failures + 1)) >"$FAIL_FILE"
	note "exited after only ${duration}s (code $code) — crash count now $((failures + 1))"
else
	note "exited after ${duration}s (code $code) — neither healthy nor a crash"
fi
exit "$code"
