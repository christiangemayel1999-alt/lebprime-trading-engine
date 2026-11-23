#!/usr/bin/env bash
# Last 12 months only (Christian, 20 Nov): tune = first 6 months, check = last 6 months.
cd "$(dirname "$0")/../.."
W=2025-11-20..2026-11-20; S=2026-05-20
for spec in "M1:148 149 150 151" "M2:142 143 144 145"; do tf=${spec%%:*}; ds=${spec#*:}
  [ -f reports/phase4/final1y_$tf/split_pooled.csv ] && continue
  mkdir -p reports/phase4/final1y_$tf
  python scripts/phase4_search.py split --window $W --split $S --settings reports/phase4/rules_final.json --datasets $ds --out reports/phase4/final1y_$tf > reports/phase4/final1y_$tf/log.txt 2>&1
  echo "$tf done $(date +%H:%M)"; done
for spec in "M1:148 149 150 151" "M2:142 143 144 145" "M3:136 137 138 139 140 141" "M5:130 131 132 133 134 135"; do tf=${spec%%:*}; ds=${spec#*:}
  python scripts/phase4_reaction_profile.py --datasets $ds --window $W --out reports/phase4/reaction/${tf}_1y.csv > reports/phase4/reaction/${tf}_1y.txt 2>&1
  echo "reaction $tf done $(date +%H:%M)"; done
echo ALL DONE $(date +%H:%M)
