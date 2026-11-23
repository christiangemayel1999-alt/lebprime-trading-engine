#!/usr/bin/env bash
# Bug fixes + zone-selection fixes, six symbols, 2020-2026 split at 2024-01-01 (tune 2020-23 / check 2024-26). Resumable.
cd "$(dirname "$0")/../.."
S=reports/phase4/rules_zonefix.json
run() { tf=$1; ds=$2; out=reports/phase4/zf_$tf; mkdir -p $out
  [ -f $out/split_pooled.csv ] && return
  python scripts/phase4_search.py split --window 2020-01-01..2026-11-21 --split 2024-01-01 --settings $S --datasets $ds --out $out >> $out/log.txt 2>&1; echo "$tf done $(date +%H:%M)"; }
run H1 "52 53 54 55 56 57"
run M15 "124 125 126 127 128 129"
echo ALL DONE $(date +%H:%M)
