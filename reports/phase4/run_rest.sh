#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while [ ! -f reports/phase4/final_M3/split_pooled.csv ]; do sleep 60; done
bash reports/phase4/run_final.sh
python scripts/phase4_reaction_profile.py --datasets 130 131 132 133 134 135 --window 2020-01-01..2026-11-20 --out reports/phase4/reaction/M5.csv
python scripts/phase4_reaction_profile.py --datasets 136 137 138 139 140 141 --window 2021-01-01..2026-11-20 --out reports/phase4/reaction/M3.csv
python scripts/phase4_reaction_profile.py --datasets 148 149 150 151 --window 2023-01-01..2026-11-20 --out reports/phase4/reaction/M1.csv
echo ALL DONE $(date +%H:%M)
