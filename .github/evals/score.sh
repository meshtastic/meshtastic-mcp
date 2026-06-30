#!/usr/bin/env bash
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
# Summarize an eval results CSV: pass rate overall and per category.
set -euo pipefail
csv="${1:-results.csv}"
[ -f "$csv" ] || { echo "usage: score.sh <results.csv>"; exit 1; }
awk -F, 'NR>1 && $3!="" {
  total++; cat[$2]++; if ($3=="PASS"){pass++; catpass[$2]++}
}
END {
  printf "overall: %d/%d PASS (%.0f%%)\n", pass, total, total?100*pass/total:0
  for (c in cat) printf "  %-10s %d/%d\n", c, catpass[c]+0, cat[c]
}' "$csv"
