#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/improve.log; do sleep 60; done
python reports/phase4/regime_m10h4.py > reports/phase4/run_m10h4.log 2>&1
echo ALL DONE $(date +%H:%M)
