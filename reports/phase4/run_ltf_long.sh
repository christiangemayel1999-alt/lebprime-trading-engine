#!/usr/bin/env bash
# Full-history rule tests per timeframe: tune 2020-2023, check 2024-2026.
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; mkdir -p reports/phase4/long_$tf
  python scripts/phase4_search.py evaluate --window 2020-01-01..2024-01-01 --settings reports/phase4/rules_v2.json --datasets $ds --out reports/phase4/long_$tf > reports/phase4/long_$tf/train.txt 2>&1
  python scripts/phase4_search.py evaluate --window 2024-01-01..2026-11-20 --settings reports/phase4/rules_v2.json --datasets $ds --out reports/phase4/long_$tf > reports/phase4/long_$tf/test.txt 2>&1
  echo "$tf done $(date +%H:%M)"; }
for spec in "$@"; do run ${spec%%:*} "${spec#*:}"; done
