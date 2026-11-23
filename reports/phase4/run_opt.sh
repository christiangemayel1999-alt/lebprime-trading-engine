#!/usr/bin/env bash
# Per-timeframe optimisation, last 12 months: tune = 20 Nov 2025 - 19 May 2026, check = 20 May - 20 Nov 2026.
cd "$(dirname "$0")/../.."
S=reports/phase4/rules_opt.json; W=2025-11-20..2026-11-20; SP=2026-05-20
run() { tf=$1; ds=$2; out=reports/phase4/opt_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window $W --split $SP --settings $S --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M15 "124 125 126 127 128 129"
run M5 "130 131 132 133 134 135"
run M3 "136 137 138 139 140 141"
run M2 "142 143 144 145"
run M1 "148 149 150 151"
echo ALL DONE $(date +%H:%M)
