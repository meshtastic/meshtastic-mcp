#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Regenerate src/meshtastic_mcp/protos/generated/ from a meshtastic/protobufs ref.
#   scripts/regen_protos.sh <ref>     # a tag, branch or commit of meshtastic/protobufs
# Same steps as meshtastic-python's bin/regen-protobufs.sh (package rename, nanopb 0.4.8
# protoc), less the nanopb option injection: the library never reads those options at
# runtime, and nanopb's protoc emits code every protobuf runtime >= 4.21 loads.
set -euo pipefail

ref=${1:?usage: scripts/regen_protos.sh <protobufs-ref>}
repo_root=$(cd "$(dirname "$0")/.." && pwd)
out="$repo_root/src/meshtastic_mcp/protos/generated"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

curl -fsSL "https://github.com/meshtastic/protobufs/archive/$ref.tar.gz" | tar -xz -C "$work"
src=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'protobufs-*' | head -n1)
sha=$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/meshtastic/protobufs/commits/$ref")

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) nanopb=nanopb-0.4.8-linux-x86 ;;
  Darwin-*) nanopb=nanopb-0.4.8-macosx-x86 ;;
  *) echo "no nanopb 0.4.8 protoc build for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
curl -fsSL "https://jpa.kapsi.fi/nanopb/download/$nanopb.tar.gz" | tar -xz -C "$work"
protoc="$work/$nanopb/generator-bin/protoc"

in="$work/in/meshtastic/protobuf"
mkdir -p "$in" "$work/out"
cp "$src"/meshtastic/*.proto "$src"/nanopb.proto "$in"
sed -i.bak -E \
  -e 's/^package meshtastic;/package meshtastic.protobuf;/' \
  -e 's/^import "meshtastic\//import "meshtastic\/protobuf\//' \
  -e 's/\(meshtastic\.([a-z][a-zA-Z0-9_]*)\)/(meshtastic.protobuf.\1)/g' \
  -e 's/^import "nanopb.proto"/import "meshtastic\/protobuf\/nanopb.proto"/' \
  "$in"/*.proto
rm -f "$in"/*.bak
"$protoc" -I="$work/in" --python_out="$work/out" "$in"/*.proto

rm -f "$out"/*_pb2.py
mkdir -p "$out"
cp "$work"/out/meshtastic/protobuf/*_pb2.py "$out"/
printf '%s\n' "$sha" >"$out/PROTOBUFS_REF"
echo "regenerated $(ls "$out"/*_pb2.py | wc -l | tr -d ' ') modules from meshtastic/protobufs $sha"
