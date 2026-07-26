#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Build a specific version of the Meshtastic-Android app from source for e2e.
#
# Pin the app to any git ref (sha / tag / branch) so a run targets an exact version, as an
# alternative to downloading a published release APK. Used by the android-e2e CI job and
# reproducible locally (needs a JDK + Android SDK; the gradlew wrapper fetches Gradle):
#
#   scripts/build_android_apk.sh --ref 2.5.20 --dest app.apk
#   scripts/build_android_apk.sh --variant assembleFdroidDebug --source-dir ~/Meshtastic-Android
#
# Prints the resolved short sha on the last line as `android-sha=<sha>`.
set -euo pipefail

REPO="https://github.com/meshtastic/Meshtastic-Android"
REF=""                          # empty -> repo default branch
VARIANT="assembleFdroidDebug"   # fdroid debug: no Play deps, TCP connect works for e2e
SRC_DIR="${MESHTASTIC_ANDROID_ROOT:-}"  # reuse existing checkout if env var set
DEST=""                         # optional: copy the built APK here

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --source-dir) SRC_DIR="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    -h|--help) sed -n '4,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SRC_DIR" ]; then
  SRC_DIR="$(mktemp -d)/Meshtastic-Android"
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
echo ">> building Meshtastic-Android ($VARIANT) at $SHA"
chmod +x ./gradlew
./gradlew --no-daemon "$VARIANT"

# Build type is the trailing PascalCase word of the variant (assembleFdroidDebug
# -> debug, assembleRelease -> release). Gradle always writes the APK under a
# .../<buildType>/ dir, so we scope the search to it — matching a release APK
# when we built debug (or vice-versa) would silently install the wrong thing.
BUILD_TYPE="$(printf '%s' "$VARIANT" | sed -E 's/.*([A-Z][a-z]+)$/\1/' | tr '[:upper:]' '[:lower:]')"

# Locate the produced APK. Prefer the authoritative path from `android describe`
# (build-target metadata incl. output artifacts) when that CLI is present and
# reports one; otherwise search the tree. Do NOT hardcode the module dir: the app
# module has moved between Meshtastic-Android layouts (`app/` -> `androidApp/`),
# which silently broke this script's old `app/build/outputs/apk` glob. Instead
# glob every `*/build/outputs/apk/**` (library modules emit .aar, not .apk, so an
# .apk match is the app), scoped to this build type, preferring a universal
# artifact so we never install an arch-specific split on the x86_64 emulator.
# androidTest / unsigned artifacts are never installable, so exclude them.
_find_apk() {  # $1 = -name pattern
  find . -type f -path '*/build/outputs/apk/*' -path "*/${BUILD_TYPE}/*" \
    -name "$1" ! -name '*unsigned*' ! -path '*androidTest*' 2>/dev/null | sort | head -1
}

APK=""
if command -v android >/dev/null 2>&1; then
  APK="$(android describe --project_dir . 2>/dev/null \
    | python3 -c 'import sys,json,re
blob=sys.stdin.read()
paths=re.findall(r"\"[^\"]*\.apk\"", blob)
cands=[p.strip(chr(34)) for p in paths if "unsigned" not in p]
univ=[p for p in cands if "universal" in p.lower()]
import os
for p in (univ or cands):
    if os.path.isfile(p): print(p); break' 2>/dev/null)"
fi
if [ -z "$APK" ]; then
  APK="$(_find_apk '*universal*.apk')"
  [ -n "$APK" ] || APK="$(_find_apk '*.apk')"
fi
[ -n "$APK" ] && [ -f "$APK" ] || {
  echo "no installable ${BUILD_TYPE} APK found under any */build/outputs/apk (variant $VARIANT)" >&2
  exit 1
}

if [ -n "$DEST" ]; then
  cp "$APK" "$DEST"
  echo ">> copied -> $DEST"
  APK="$DEST"
fi

echo ">> built: $(cd "$(dirname "$APK")" && pwd)/$(basename "$APK")"
echo "android-sha=$SHA"
