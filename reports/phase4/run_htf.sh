#!/usr/bin/env bash
# Matched higher-TF trend per style. Three periods: 2026 (split Aug), 2025 (split Jul), older (two halves). Resumable.
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/exits.log 2>/dev/null; do sleep 60; done
run() { style=$1; tf=$2; ds=$3; old=$4; oldsp=$5; for per in 2026 2025 old; do
    case $per in 2026) win=2026-01-01..2026-11-22; sp=2026-08-01;; 2025) win=2025-01-01..2026-01-01; sp=2025-07-01;; old) win=$old; sp=$oldsp;; esac
    out=reports/phase4/htf/${tf}_$per; mkdir -p $out
    [ -f $out/split_pooled.csv ] || python scripts/phase4_search.py split --window $win --split $sp --settings reports/phase4/rules_htf_$style.json --datasets $ds --out $out >> $out/log.txt 2>&1
  done; echo "$tf done $(date +%H:%M)"; }
run long H4 "157 158 159 160 161 162" 2020-01-01..2025-01-01 2022-07-01
run long H1 "52 53 54 55 56 57" 2020-01-01..2025-01-01 2022-07-01
run swing M30 "58 59 60 61 62 63" 2020-01-01..2025-01-01 2022-07-01
run swing M15 "124 125 126 127 128 129" 2020-01-01..2025-01-01 2022-07-01
run swing M10 "163 164 165 166 167 168" 2020-01-01..2025-01-01 2022-07-01
run swing M5 "130 131 132 133 134 135" 2020-01-01..2025-01-01 2022-07-01
run scalp M3 "136 137 138 139 140 141" 2021-01-01..2025-01-01 2023-01-01
run scalp M2 "142 143 144 145" 2022-01-01..2025-01-01 2023-07-01
run scalp M1 "148 149 150 151" 2023-01-01..2025-01-01 2024-01-01
echo ALL DONE $(date +%H:%M)
