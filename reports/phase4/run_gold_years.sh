#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
run() { tf=$1; ds=$2; out=reports/phase4/goldyears_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2020-01-01..2025-01-01 --split 2022-07-01 --settings reports/phase4/rules_gold_final_$tf.json --datasets $ds --out $out --workers 2 >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run M5 130; run M15 124
echo ALL DONE $(date +%H:%M)
