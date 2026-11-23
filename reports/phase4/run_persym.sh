#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_expiry.log 2>/dev/null || ! grep -q "ALL DONE" reports/phase4/run_gold_years.log 2>/dev/null; do sleep 30; done
python reports/phase4/persym_pipeline.py
