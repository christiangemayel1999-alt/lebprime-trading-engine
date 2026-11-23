#!/usr/bin/env bash
# One run per setting x symbol over the window; results split into tune (before 2024) and check (2024-2026).
cd "$(dirname "$0")/../.."
run() { tf=$1; win=$2; ds=$3; mkdir -p reports/phase4/final_$tf
  [ -f reports/phase4/final_$tf/split_pooled.csv ] && { echo "$tf already done"; return; }
  python scripts/phase4_search.py split --window "$win" --split 2024-01-01 --settings reports/phase4/rules_final.json --datasets $ds --out reports/phase4/final_$tf > reports/phase4/final_$tf/log.txt 2>&1
  echo "$tf done $(date +%H:%M)"; }
run M15 2020-01-01..2026-11-20 "124 125 126 127 128 129"
run M5 2020-01-01..2026-11-20 "130 131 132 133 134 135"
run M3 2021-01-01..2026-11-20 "136 137 138 139 140 141"
run M1 2023-01-01..2026-11-20 "148 149 150 151"
run M2 2022-01-01..2026-11-20 "142 143 144 145"
