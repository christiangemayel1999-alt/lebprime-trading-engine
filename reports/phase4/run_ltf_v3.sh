#!/usr/bin/env bash
# Christian's Entry 2 recovery rule, close confirmation, and momentum / ATR / lookback variants per timeframe.
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; train=$3; mkdir -p reports/phase4/v3_$tf
  python scripts/phase4_search.py evaluate --window "$train" --settings reports/phase4/rules_v3.json --datasets $ds --out reports/phase4/v3_$tf > reports/phase4/v3_$tf/train.txt 2>&1
  python scripts/phase4_search.py evaluate --window 2024-01-01..2026-11-20 --settings reports/phase4/rules_v3.json --datasets $ds --out reports/phase4/v3_$tf > reports/phase4/v3_$tf/test.txt 2>&1
  echo "$tf done $(date +%H:%M)"; }
run M15 "124 125 126 127 128 129" 2020-01-01..2024-01-01
run M5 "130 131 132 133 134 135" 2020-01-01..2024-01-01
run M3 "136 137 138 139 140 141" 2020-01-01..2024-01-01
run M2 "142 143 144 145 146 147" 2022-01-01..2024-01-01
run M1 "148 149 150 151 152 153" 2022-01-01..2024-01-01
