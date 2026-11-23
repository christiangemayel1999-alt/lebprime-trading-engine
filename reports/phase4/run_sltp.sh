#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/exits.log 2>/dev/null; do sleep 60; done
python reports/phase4/sltp.py > reports/phase4/sltp.log 2>&1
echo ALL DONE $(date +%H:%M)
