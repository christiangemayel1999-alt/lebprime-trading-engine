#!/usr/bin/env bash
# Check the two recent-window candidates on Jan 2025 - May 2026 (never used for tuning). Resumable.
cd "$(dirname "$0")/../.."
S=reports/phase4/rules_recent_cand.json
run() { tf=$1; ds=$2; out=reports/phase4/cand_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2025-01-01..2026-06-01 --split 2025-09-01 --settings $S --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M5 "130 131 132 133 134 135"
run M3 "136 137 138 139 140 141"
run M1 "148 149 150 151"
echo ALL DONE $(date +%H:%M)
