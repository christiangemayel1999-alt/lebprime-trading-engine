#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_resume_all.log; do sleep 60; done
python reports/phase4/regime_2026.py > reports/phase4/run_regime.log 2>&1
echo ALL DONE $(date +%H:%M)
