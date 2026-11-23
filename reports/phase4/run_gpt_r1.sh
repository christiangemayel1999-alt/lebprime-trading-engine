#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
S=reports/phase4/rules_gpt_r1.json
run() { name=$1; win=$2; sp=$3; ds=$4; out=reports/phase4/gpt_r1/$name; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window $win --split $sp --settings $S --datasets $ds --out $out --workers 4 >> $out/log.txt 2>&1; echo "$name done $(date +%H:%M)"; }
run M5_recent 2026-06-01..2026-11-21 2026-11-01 "130 131 132 133 134 135"
run M15_recent 2026-06-01..2026-11-21 2026-11-01 "124 125 126 127 128 129"
run M5_2025 2025-01-01..2026-06-01 2025-09-01 "130 131 132 133 134 135"
run M15_2025 2025-01-01..2026-06-01 2025-09-01 "124 125 126 127 128 129"
run M5_old 2020-01-01..2025-01-01 2022-07-01 "130 131 132 133 134 135"
run M15_old 2020-01-01..2025-01-01 2022-07-01 "124 125 126 127 128 129"
echo ALL DONE $(date +%H:%M)
