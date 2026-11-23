#!/usr/bin/env bash
cd "$(dirname "$0")/../.."
while ! grep -q "ALL DONE" reports/phase4/run_gpt_r1.log; do sleep 30; done
out=reports/phase4/g1var; mkdir -p $out
[ -f $out/split_pooled.csv ] || python scripts/phase4_search.py split --window 2020-01-01..2026-11-21 --split 2023-07-01 --settings reports/phase4/rules_g1_variants.json --datasets 130 --out $out >> $out/log.txt 2>&1
echo ALL DONE $(date +%H:%M)
