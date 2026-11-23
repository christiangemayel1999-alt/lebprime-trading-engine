#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
S=reports/phase4/rules_rootcause.json
run() { tf=$1; win=$2; split=$3; ds=$4; out=reports/phase4/rc_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window $win --split $split --settings $S --datasets $ds --out $out > $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M15 2020-01-01..2026-11-20 2024-01-01 "124 125 126 127 128 129"
run M5 2025-11-20..2026-11-20 2026-05-20 "130 131 132 133 134 135"
run M1 2025-11-20..2026-11-20 2026-05-20 "148 149 150 151"
run M3 2025-11-20..2026-11-20 2026-05-20 "136 137 138 139 140 141"
echo ALL DONE $(date +%H:%M)
