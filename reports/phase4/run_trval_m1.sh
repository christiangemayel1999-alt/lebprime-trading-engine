#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_trval.log; do sleep 60; done
run() { per=$1; win=$2; sp=$3; out=reports/phase4/trval/M1_$per; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window $win --split $sp --settings reports/phase4/rules_trval_M1.json --datasets 148 149 150 151 --out $out >> $out/log.txt 2>&1; echo "M1 $per done $(date +%H:%M)"; }
run 2025 2025-01-01..2026-06-01 2025-09-01
run old 2023-01-01..2025-01-01 2024-01-01
echo ALL DONE $(date +%H:%M)
