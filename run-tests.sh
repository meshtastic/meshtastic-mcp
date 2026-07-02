#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
# meshtastic-mcp hardware test runner.
#
# Auto-detects connected Meshtastic devices, maps each to its PlatformIO env
# via the same role table the pytest fixtures use, exports the right
# MESHTASTIC_MCP_ENV_* env vars, and invokes pytest.
#
# Usage:
#   ./run-tests.sh                        # full suite, default pytest args
#   ./run-tests.sh tests/mesh             # subset (any pytest args pass through)
#   ./run-tests.sh --force-bake           # override one default with another
#   MESHTASTIC_MCP_ENV_NRF52=foo ./run-tests.sh   # override env per role
#   MESHTASTIC_MCP_SEED=ci-run-42 ./run-tests.sh  # override PSK seed
#
# If zero supported devices are detected, only the unit tier runs.
#
# Also restores `userPrefs.jsonc` from the session-backup sidecar if a prior
# run exited abnormally (belt to conftest.py's atexit suspenders).

set -euo pipefail

# cd to the script's directory so relative paths resolve consistently no
# matter where the user invoked from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x $VENV_PY ]]; then
	echo "error: $VENV_PY not found or not executable." >&2
	echo "       Bootstrap the venv first:" >&2
	echo "         cd $SCRIPT_DIR && python3 -m venv .venv && .venv/bin/pip install -e '.[test]'" >&2
	exit 2
fi

# Resolve firmware root from the environment (standalone package — no fixed firmware
# layout). The firmware tier needs it; the portable/hardware tiers don't. Falls back to
# the parent dir for the legacy in-firmware-tree layout.
FIRMWARE_ROOT="${MESHTASTIC_FIRMWARE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
USERPREFS_PATH="$FIRMWARE_ROOT/userPrefs.jsonc"
USERPREFS_SIDECAR="$USERPREFS_PATH.mcp-session-bak"

# ---------- Pre-flight: recover stale userPrefs.jsonc from prior crash ----
# If conftest.py's atexit hook didn't fire (SIGKILL, kernel panic, OS
# restart), the sidecar is the ground truth. Self-heal before running so we
# don't bake the previous run's dirty state into this run's firmware.
if [[ -f $USERPREFS_SIDECAR ]]; then
	echo "[pre-flight] found $USERPREFS_SIDECAR from a prior abnormal exit;" >&2
	echo "             restoring userPrefs.jsonc before starting." >&2
	cp "$USERPREFS_SIDECAR" "$USERPREFS_PATH"
	rm -f "$USERPREFS_SIDECAR"
fi

# If userPrefs.jsonc has uncommitted changes BEFORE the run starts, that's
# worth warning about — tests will snapshot this dirty state and restore to
# it at the end, which may not be what the operator wants.
if command -v git >/dev/null 2>&1; then
	cd "$FIRMWARE_ROOT"
	# Capture the git status into a local first — SC2312 flags command
	# substitution inside `[[ -n ... ]]` because the exit code of `git
	# status` is masked. A two-step assignment makes the failure path
	# explicit (non-git, missing file) and keeps the bracket test clean.
	_git_status_porcelain="$(git status --porcelain userPrefs.jsonc 2>/dev/null || true)"
	if [[ -n $_git_status_porcelain ]]; then
		echo "[pre-flight] warning: userPrefs.jsonc has uncommitted changes." >&2
		echo "             Tests will snapshot THIS state and restore to it" >&2
		echo "             at teardown. If that's not intended, run:" >&2
		echo "               git checkout userPrefs.jsonc" >&2
		echo "             and re-invoke." >&2
	fi
	cd "$SCRIPT_DIR"
fi

# ---------- Seed default --------------------------------------------------
# Per-machine default so repeated runs from the same operator land on the
# same PSK (makes --assume-baked valid across invocations). Operator can
# override with an explicit env var if they want isolation (e.g. CI).
if [[ -z ${MESHTASTIC_MCP_SEED-} ]]; then
	WHO="$(whoami 2>/dev/null || echo anon)"
	HOST="$(hostname -s 2>/dev/null || echo host)"
	export MESHTASTIC_MCP_SEED="mcp-${WHO}-${HOST}"
fi

# ---------- Flash progress log --------------------------------------------
# pio.py / hw_tools.py tee subprocess output (pio run -t upload, esptool,
# nrfutil, picotool) to this file line-by-line as it arrives when this env
# var is set. The TUI tails it so the operator sees live flash progress
# instead of 3 minutes of silence during `test_00_bake.py`. Plain CLI users
# also benefit — the log is a post-run diagnostic even without the TUI.
# Truncate at session start so each run gets a clean log.
export MESHTASTIC_MCP_FLASH_LOG="$SCRIPT_DIR/tests/flash.log"
: >"$MESHTASTIC_MCP_FLASH_LOG"

# ---------- Detect connected hardware -------------------------------------
# Per-board bench roles (tests/_bench.py). Assignment order per role:
#   1. hub-slot location match (the reference bench's pinned slots)
#   2. VERIFIED: a device_info handshake's hw_model resolves to the role's env
#   3. VID fallback (UNVERIFIED — same-VID boards are indistinguishable here)
# The exported MESHTASTIC_MCP_ENV_<ROLE> always prefers the env resolved from
# the board's ACTUAL hw_model, so a fallback mislabel can't bake the wrong
# variant. Operator MESHTASTIC_MCP_ENV_<ROLE> overrides win over everything.
DETECTED=""
UNVERIFIED=0
while IFS=$'\t' read -r role port env verified; do
	[[ -z $role ]] && continue
	upper="$(echo "$role" | tr '[:lower:]' '[:upper:]')"
	var="MESHTASTIC_MCP_ENV_${upper}"
	eval "override=\${$var:-}"
	if [[ -n $override ]]; then
		env="$override"
		verified="operator-override"
	fi
	export "MESHTASTIC_MCP_ENV_${upper}=$env"
	if [[ $verified == "unverified" ]]; then
		UNVERIFIED=1
	fi
	DETECTED="${DETECTED}  $(printf '%-12s' "$role") @ ${port} -> env=${env} [${verified}]\n"
done < <(
	"$VENV_PY" - <<'PY'
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")
from tests import _bench  # noqa: E402
from meshtastic_mcp import devices, info  # noqa: E402
from meshtastic_mcp.web.services import identity  # noqa: E402  # env_for_hw_model (no web deps)

devs = [d for d in devices.list_devices(include_unknown=True) if d.get("port")]


def vid_of(d) -> int | None:
    raw = d.get("vid") or ""
    try:
        return int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return None


# Ground truth per port: a short device_info handshake -> hw_model -> exact env.
# Fails soft (busy port / non-meshtastic device / no firmware root).
exact_env: dict[str, str] = {}
for d in devs:
    if not (d.get("likely_meshtastic") or identity.role_for_vid(d.get("vid"))):
        continue
    try:
        di = info.device_info(port=d["port"], timeout_s=8.0)
        env = identity.env_for_hw_model(di.get("hw_model"))
        if env:
            exact_env[d["port"]] = env
    except Exception as exc:
        print(f"note: handshake failed on {d['port']}: {exc}", file=sys.stderr)

used: set[str] = set()
assigned: dict[str, tuple[str, str, str]] = {}  # role -> (port, env, verified)

# Pass 1 — location pins, then hw_model-verified env matches.
for role in _bench.roles():
    spec_loc = _bench.role_location(role)
    if spec_loc:
        for d in devs:
            p = d["port"]
            if p not in used and _bench.device_location(p) == spec_loc:
                env = exact_env.get(p) or _bench.role_env(role) or ""
                assigned[role] = (p, env, "hub-slot")
                used.add(p)
                break
for role in _bench.roles():
    if role in assigned:
        continue
    want = _bench.role_env(role)
    for p, env in exact_env.items():
        if p not in used and env == want:
            assigned[role] = (p, env, "verified")
            used.add(p)
            break

# Pass 2 — VID fallback for whatever's left. If the board handshook, trust its
# hw_model env over the role default; otherwise flag it loudly.
for role in _bench.roles():
    if role in assigned:
        continue
    vids = set(_bench.role_vids(role))
    for d in devs:
        p = d["port"]
        if p in used or vid_of(d) not in vids:
            continue
        if p in exact_env:
            assigned[role] = (p, exact_env[p], "verified")
        else:
            assigned[role] = (p, _bench.role_env(role) or "", "unverified")
        used.add(p)
        break

for role, (p, env, verified) in assigned.items():
    print(f"{role}\t{p}\t{env}\t{verified}")
PY
)

if [[ $UNVERIFIED == 1 ]]; then
	echo "WARNING: some roles are VID-guessed with no hw_model handshake — the"
	echo "         bake would flash that role's DEFAULT env. Verify the board or"
	echo "         set MESHTASTIC_MCP_ENV_<ROLE> before trusting a flash."
fi

# ---------- Pre-flight summary --------------------------------------------
# Surface what pytest is about to do with respect to the bake phase: the
# operator should see "will verify + bake if needed" by default, so a
# 3-minute flash appearing mid-run isn't a surprise. Detection of the
# explicit overrides is best-effort — we just scan $@ for the known flags.
_bake_mode="auto (verify + bake if needed)"
for _arg in "$@"; do
	case "$_arg" in
	--assume-baked) _bake_mode="skip (--assume-baked)" ;;
	--force-bake) _bake_mode="force (--force-bake)" ;;
	*) ;; # any other arg: pass-through; bake mode unchanged
	esac
done

echo "meshtastic-mcp test runner"
echo "  firmware root : $FIRMWARE_ROOT"
echo "  seed          : $MESHTASTIC_MCP_SEED"
echo "  bake          : $_bake_mode"
if [[ -n $DETECTED ]]; then
	echo "  detected hub  :"
	printf "%b" "$DETECTED"
else
	echo "  detected hub  : (none)"
fi
echo

# ---------- Invoke pytest -------------------------------------------------
# If no devices detected, only the unit tier would produce meaningful
# PASS/FAIL — every hardware test would SKIP with "role not present". We
# narrow to tests/unit explicitly so the summary reads as "no hardware,
# unit suite only" instead of "big skip count looks suspicious".
# Keep terminal output condensed (`-q -r fE`) so skip-heavy runs do not print
# each skipped test in full; skip counts still appear in pytest's summary.
if [[ -z $DETECTED && $# -eq 0 ]]; then
	echo "[pre-flight] no supported devices detected; running unit tier only."
	echo
	exec "$VENV_PY" -m pytest tests/unit -q -r fE --report-log=tests/reportlog.jsonl
fi

# Default pytest args when the user passed none. Power users can invoke
# `./run-tests.sh tests/mesh -v --tb=long` and skip all of these defaults.
#
# NOTE: `--assume-baked` is DELIBERATELY omitted here. `tests/test_00_bake.py`
# has an internal skip-if-already-baked check (`_bake_role`: query device_info,
# compare region + primary_channel to the session profile, skip on match).
# So the fast path is ~8-10 s of verification overhead when the devices are
# already baked — negligible next to the 2-6 min suite runtime. Letting
# test_00_bake.py run means a fresh device, a re-seeded session, or a post-
# factory-reset device gets flashed automatically instead of silently
# skipping half the hardware tests with "not baked with session profile"
# errors. Power users who know their hardware is current and want to shave
# those seconds can pass `--assume-baked` explicitly.
# Defaults also use condensed reporting (`-q -r fE`) to avoid listing every
# skipped test verbatim while still surfacing failures/errors and summary data.
if [[ $# -eq 0 ]]; then
	set -- tests/ \
		--html=tests/report.html --self-contained-html \
		--junitxml=tests/junit.xml \
		-q -r fE --tb=short
fi

# UI tier requires opencv-python-headless (and ideally easyocr). If it's
# not installed, auto-deselect tests/ui so operators without the [ui]
# extra still get a green run. Printed in yellow; silent when cv2 is
# present.
_cv2_ok=0
if "$VENV_PY" -c "import cv2" >/dev/null 2>&1; then
	_cv2_ok=1
fi
_running_ui=0
for _arg in "$@"; do
	case "$_arg" in
	*tests/ui* | tests/) _running_ui=1 ;;
	*) ;;
	esac
done
if [[ $_running_ui -eq 1 && $_cv2_ok -eq 0 ]]; then
	printf '\033[33m[pre-flight] tests/ui tier detected, but opencv-python-headless is not installed — deselecting.\033[0m\n'
	printf '             install with: .venv/bin/pip install -e ".[ui]"\n'
	echo
	set -- "$@" --ignore=tests/ui
fi

# Recovery tier needs `uhubctl` on PATH — it power-cycles devices via USB
# hub PPPS. The tier's conftest already skips cleanly, so this is just a
# friendly heads-up before the skip happens. `baked_single`'s auto-
# recovery hook also benefits from having uhubctl available across the
# whole suite.
if ! command -v uhubctl >/dev/null 2>&1; then
	printf "\033[33m[pre-flight] uhubctl not found on PATH — recovery tier will skip, and\n"
	printf "             wedged-device auto-recovery is disabled.\033[0m\n"
	printf "             install with: brew install uhubctl (macOS) or apt install uhubctl (Debian/Ubuntu).\n"
	echo
fi

# Always emit `tests/reportlog.jsonl` (unless the operator explicitly passed
# their own `--report-log=...`). Consumers — notably the
# `meshtastic-mcp-test-tui` TUI — tail the reportlog for live per-test state.
# Appending here means power-user invocations like `./run-tests.sh tests/mesh`
# also produce it, not just the all-defaults invocation.
_has_report_log=0
for _arg in "$@"; do
	case "$_arg" in
	--report-log | --report-log=*) _has_report_log=1 ;;
	*) ;; # any other arg: no-op; loop continues
	esac
done
if [[ $_has_report_log -eq 0 ]]; then
	set -- "$@" --report-log=tests/reportlog.jsonl
fi

exec "$VENV_PY" -m pytest "$@"
