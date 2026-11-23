#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_cand.log; do sleep 30; done
run() { tf=$1; ds=$2; out=reports/phase4/goldcand_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2025-01-01..2026-06-01 --split 2025-09-01 --settings reports/phase4/rules_gold_cand_$tf.json --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M15 124; run M5 130; run M3 136; run M1 148
echo ALL DONE $(date +%H:%M)
