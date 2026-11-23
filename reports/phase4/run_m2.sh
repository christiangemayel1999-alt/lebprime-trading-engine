#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_ustec.log; do sleep 60; done
python reports/phase4/persym_ustec_m2.py > reports/phase4/run_ustec_m2.log 2>&1
python reports/phase4/persym_m2.py > reports/phase4/run_persym_m2.log 2>&1
echo ALL DONE $(date +%H:%M)
