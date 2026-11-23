#!/usr/bin/env bash
# Fast loop: tune Jun-Oct 2026, check 1-20 Nov 2026 (unseen). Resumable.
cd "$(dirname "$0")/../.."
S=${1:-reports/phase4/rules_zonefix.json}; TAG=${2:-zf}
run() { tf=$1; ds=$2; out=reports/phase4/recent_${TAG}_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2026-06-01..2026-11-21 --split 2026-11-01 --settings $S --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M15 "124 125 126 127 128 129"
run M5 "130 131 132 133 134 135"
run M3 "136 137 138 139 140 141"
run M1 "148 149 150 151"
echo ALL DONE $(date +%H:%M)
