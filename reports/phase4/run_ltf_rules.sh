#!/usr/bin/env bash
# Rule-fix tests per low timeframe: tune window (older ~60%) vs check window (newest ~40%).
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; train=$3; test=$4
  python scripts/phase4_search.py evaluate --window "$train" --settings reports/phase4/rules_v1.json --datasets $ds --out reports/phase4/ltf_$tf > /dev/null 2>&1
  python scripts/phase4_search.py evaluate --window "$test" --settings reports/phase4/rules_v1.json --datasets $ds --out reports/phase4/ltf_$tf > /dev/null 2>&1
  echo "$tf done"; }
run M1 "70 71 72 73 74 75" 2026-08-10..2026-10-10 2026-10-10..2026-11-20
run M2 "76 77 78 79 80 81" 2026-04-29..2026-08-15 2026-08-15..2026-11-20
run M3 "82 83 84 85 86 87" 2026-01-16..2026-07-01 2026-07-01..2026-11-20
run M5 "88 89 90 91 92 93" 2025-06-24..2026-04-01 2026-04-01..2026-11-20
run M15 "64 65 66 67 68 69" 2022-08-23..2025-01-01 2025-01-01..2026-11-20
