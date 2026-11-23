#!/usr/bin/env bash
# Sweep + reclaim (the entry that works on US30 M5 / USTEC M30) on every symbol and timeframe, 3 periods. Resumable.
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; old=$3; oldsp=$4; for per in 2026 2025 old; do
    case $per in 2026) win=2026-01-01..2026-11-22; sp=2026-08-01;; 2025) win=2025-01-01..2026-01-01; sp=2025-07-01;; old) win=$old; sp=$oldsp;; esac
    out=reports/phase4/sweep_all/${tf}_$per; mkdir -p $out
    [ -f $out/split_pooled.csv ] || python scripts/phase4_search.py split --window $win --split $sp --settings reports/phase4/rules_sweep_all.json --datasets $ds --out $out >> $out/log.txt 2>&1
  done; echo "$tf done $(date +%H:%M)"; }
run H1 "52 53 54 55 56 57" 2020-01-01..2025-01-01 2022-07-01
run M30 "58 59 60 61 62 63" 2020-01-01..2025-01-01 2022-07-01
run M10 "163 164 165 166 167 168" 2020-01-01..2025-01-01 2022-07-01
run M3 "136 137 138 139 140 141" 2021-01-01..2025-01-01 2023-01-01
run M2 "142 143 144 145" 2022-01-01..2025-01-01 2023-07-01
run M1 "148 149 150 151" 2023-01-01..2025-01-01 2024-01-01
echo ALL DONE $(date +%H:%M)
