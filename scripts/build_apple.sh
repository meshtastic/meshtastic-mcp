#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Build the Meshtastic iOS-Simulator .app from a specific git ref (or an existing checkout).
#
# Pin the app to any git ref (sha / tag / branch) so a run targets an exact version.
# Used by the apple-e2e CI job and reproducible locally (needs Xcode + watchOS runtime):
#
#   scripts/build_apple.sh --ref 2.7.4 --dest ./Meshtastic.app
#   scripts/build_apple.sh --source-dir ~/Meshtastic-Apple --sim "iPhone 16 Pro"
#
# MESHTASTIC_APPLE_ROOT is used as the default --source-dir if set (dev workflow).
#
# Prints the resolved short sha on the last line as `apple-sha=<sha>` so callers
# (CI step summaries, the e2e verdict) can record exactly what was tested.
set -euo pipefail

REPO="https://github.com/meshtastic/Meshtastic-Apple"
REF=""                               # empty -> repo default branch
SRC_DIR="${MESHTASTIC_APPLE_ROOT:-}" # reuse existing checkout if env var is set
SIM="iPhone 16 Pro"                  # -destination simulator name
DEST=""                              # optional: path to copy the .app bundle

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --source-dir) SRC_DIR="$2"; shift 2 ;;
    --sim) SIM="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    -h|--help) sed -n '4,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Acquire the source tree (clone fresh, or reuse + fetch the ref).
if [ -z "$SRC_DIR" ]; then
  SRC_DIR="$(mktemp -d)/Meshtastic-Apple"
  echo ">> cloning $REPO -> $SRC_DIR"
  git clone --recurse-submodules "$REPO" "$SRC_DIR"
fi
cd "$SRC_DIR"

if [ -n "$REF" ]; then
  echo ">> checking out $REF"
  git fetch --tags --recurse-submodules origin "$REF" || git fetch --tags origin
  git checkout "$REF"
  git submodule update --init --recursive
fi

SHA="$(git rev-parse --short HEAD)"
echo ">> building Meshtastic.app (sim='$SIM') at $SHA"

# Download watchOS runtime — required by the scheme (embeds the Watch app).
# This is a no-op if already installed, but may take a few minutes the first time.
echo ">> ensuring watchOS simulator runtime"
xcodebuild -downloadPlatform watchOS

# Build the iOS-Simulator .app with ad-hoc signing that keeps entitlements.
xcodebuild \
  -workspace Meshtastic.xcworkspace \
  -scheme Meshtastic \
  -destination "platform=iOS Simulator,name=${SIM}" \
  -derivedDataPath build \
  CODE_SIGN_IDENTITY="-" \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGNING_ALLOWED=YES \
  AD_HOC_CODE_SIGNING_ALLOWED=YES \
  build

APP="$SRC_DIR/build/Build/Products/Debug-iphonesimulator/Meshtastic.app"
[ -d "$APP" ] || { echo "build produced no .app at $APP" >&2; exit 1; }

if [ -n "$DEST" ]; then
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  echo ">> copied -> $DEST"
  APP="$DEST"
fi

echo ">> built: $APP"
# Machine-readable provenance (grep-able by CI / the e2e harness):
echo "apple-sha=$SHA"
