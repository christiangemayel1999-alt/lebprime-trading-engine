#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_gold_cand.log 2>/dev/null; do sleep 30; done
run() { tf=$1; ds=$2; out=reports/phase4/expiry_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2026-06-01..2026-11-21 --split 2026-11-01 --settings reports/phase4/rules_expiry_$tf.json --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M3 "136 137 138 139 140 141"; run M2 "142 143 144 145"; run M1 "148 149 150 151"
echo ALL DONE $(date +%H:%M)
