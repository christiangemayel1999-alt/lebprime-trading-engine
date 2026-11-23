#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; for per in 2026 2025 old; do
    case $per in 2026) win=2026-01-01..2026-11-22; sp=2026-08-01;; 2025) win=2025-01-01..2026-01-01; sp=2025-07-01;; old) win=2020-01-01..2025-01-01; sp=2022-07-01;; esac
    out=reports/phase4/htf2/${tf}_$per; mkdir -p $out
    [ -f $out/split_pooled.csv ] || python scripts/phase4_search.py split --window $win --split $sp --settings reports/phase4/rules_htf_swing_m15.json --datasets $ds --out $out >> $out/log.txt 2>&1
  done; echo "$tf done $(date +%H:%M)"; }
run M30 "58 59 60 61 62 63"
run M10 "163 164 165 166 167 168"
run M5 "130 131 132 133 134 135"
echo ALL DONE $(date +%H:%M)
