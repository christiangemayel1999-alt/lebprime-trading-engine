#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
run() { tf=$1; per=$2; win=$3; sp=$4; ds=$5; out=reports/phase4/trval/${tf}_$per; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window $win --split $sp --settings reports/phase4/rules_trval_$tf.json --datasets $ds --out $out --workers 4 >> $out/log.txt 2>&1; echo "$tf $per done $(date +%H:%M)"; }
run M3 2025 2025-01-01..2026-06-01 2025-09-01 "136 137 138 139 140 141"
run M5 2025 2025-01-01..2026-06-01 2025-09-01 "130 131 132 133 134 135"
run M15 2025 2025-01-01..2026-06-01 2025-09-01 "124 125 126 127 128 129"
run M3 old 2021-01-01..2025-01-01 2023-01-01 "136 137 138 139 140 141"
run M5 old 2020-01-01..2025-01-01 2022-07-01 "130 131 132 133 134 135"
run M15 old 2020-01-01..2025-01-01 2022-07-01 "124 125 126 127 128 129"
echo ALL DONE $(date +%H:%M)
