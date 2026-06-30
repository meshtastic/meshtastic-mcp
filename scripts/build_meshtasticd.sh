#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Build a specific version of native `meshtasticd` for the hardware-free e2e mesh.
#
# Pin the firmware to any git ref (sha / tag / branch) so a run targets an exact version.
# Used by the apple-e2e / meshtasticd-native CI jobs and reproducible locally:
#
#   scripts/build_meshtasticd.sh --env native        --ref v2.7.4
#   scripts/build_meshtasticd.sh --env native-macos  --ref 1a2b3c4 --dest ./meshtasticd
#
# Prints the resolved short sha on the last line as `meshtasticd-sha=<sha>` so callers
# (CI step summaries, the e2e verdict) can record exactly what was tested.
set -euo pipefail

REPO="https://github.com/meshtastic/firmware"
REF=""                       # empty -> repo default branch
ENV="native"                 # native (Linux) | native-macos (Darwin, needs the #75 multicast fix)
FW_DIR="${MESHTASTIC_FIRMWARE_ROOT:-}"   # reuse an existing checkout if provided
DEST=""                      # optional: copy the built binary here

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --env) ENV="$2"; shift 2 ;;
    --firmware-dir) FW_DIR="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    -h|--help) sed -n '4,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Acquire the firmware checkout (clone fresh, or reuse + fetch the ref).
if [ -z "$FW_DIR" ]; then
  FW_DIR="$(mktemp -d)/firmware"
  echo ">> cloning $REPO -> $FW_DIR"
  git clone --recurse-submodules "$REPO" "$FW_DIR"
fi
cd "$FW_DIR"

if [ -n "$REF" ]; then
  echo ">> checking out $REF"
  git fetch --tags --recurse-submodules origin "$REF" || git fetch --tags origin
  git checkout --recurse-submodules "$REF" 2>/dev/null || git checkout "$REF"
  git submodule update --init --recursive
fi

SHA="$(git rev-parse --short HEAD)"
echo ">> building meshtasticd (env=$ENV) at $SHA"
command -v pio >/dev/null 2>&1 || pip install --upgrade platformio
pio run -e "$ENV"

BIN=".pio/build/${ENV}/meshtasticd"
[ -f "$BIN" ] || { echo "build produced no binary at $BIN" >&2; exit 1; }
if [ -n "$DEST" ]; then
  cp "$BIN" "$DEST"
  echo ">> copied -> $DEST"
  BIN="$DEST"
fi

echo ">> built: $(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
# Machine-readable provenance (grep-able by CI / the e2e harness):
echo "meshtasticd-sha=$SHA"
