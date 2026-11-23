#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
python reports/phase4/persym_ustec.py > reports/phase4/run_ustec.log 2>&1
python reports/phase4/persym_ustec_m2.py > reports/phase4/run_ustec_m2.log 2>&1
bash reports/phase4/run_trval_m1.sh >> reports/phase4/run_trval_m1.log 2>&1
python scripts/phase4_search.py split --window 2025-01-01..2026-06-01 --split 2025-09-01 --settings reports/phase4/rules_trval_M3.json --datasets 136 --out reports/phase4/trval/M3_gold_2025 > /dev/null 2>&1
python reports/phase4/persym_m2.py > reports/phase4/run_persym_m2.log 2>&1
echo ALL DONE $(date +%H:%M)
