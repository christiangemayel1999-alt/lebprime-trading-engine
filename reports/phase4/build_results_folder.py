"""Collect every split test into LebPrime_Results/all_results.csv (re-run to refresh)."""
import sys; sys.path.insert(0, ".")
import glob, json, os
import pandas as pd
P = "reports/phase4"
ROOT_R = "LebPrime AI Research"; OUT = ROOT_R + "/02 - Backtest Results"; SET = ROOT_R + "/01 - Strategies & Settings"; os.makedirs(OUT, exist_ok=True)
# test folder prefix -> (description, tune period, check period)
META = {
 "final_M15": ("16 rule variants, 6 symbols", "2020-01..2023-12", "2024-01..2026-11"),
 "final_M5": ("16 rule variants, 6 symbols", "2020-01..2023-12", "2024-01..2026-11"),
 "final_M3": ("16 rule variants, 6 symbols", "2021-01..2023-12", "2024-01..2026-11"),
 "final1y_M1": ("16 rule variants, 4 symbols", "2025-11-20..2026-05-19", "2026-05-20..2026-11-20"),
 "final1y_M2": ("16 rule variants, 4 symbols", "2025-11-20..2026-05-19", "2026-05-20..2026-11-20"),
 "combo_M15": ("filter combinations", "2020-01..2023-12", "2024-01..2026-11"),
 "combo_M5": ("filter combinations", "2020-01..2023-12", "2024-01..2026-11"),
 "quality_M15": ("RSI / early-exit rules", "2020-01..2023-12", "2024-01..2026-11"),
 "structure_M15": ("structure early exits", "2020-01..2023-12", "2024-01..2026-11"),
 "rc_M15": ("stop redesign (retest stop, emergency stop)", "2020-01..2023-12", "2024-01..2026-11"),
 "rc_M5": ("stop redesign (partial)", "2025-11-20..2026-05-19", "2026-05-20..2026-11-20"),
 "opt_": ("60-setting random search", "2025-11-20..2026-05-19", "2026-05-20..2026-11-20"),
 "val_M5": ("M5 candidates on older data", "2020-01..2022-12", "2023-01..2025-11"),
 "val_M3": ("M3 candidates on older data", "2021-01..2023-05", "2023-06..2025-11"),
 "val_gold_m15": ("Gold M15 candidates on older data", "2020-01..2022-12", "2023-01..2025-11"),
 "zf_": ("bug fixes + zone fixes (6y, stopped early)", "2020-01..2023-12", "2024-01..2026-11"),
 "recent_zf_": ("bug fixes + zone fixes", "2026-06-01..2026-10-31", "2026-11-01..2026-11-20"),
 "recent_ro_": ("60-setting search on H4 + real base", "2026-06-01..2026-10-31", "2026-11-01..2026-11-20"),
 "cand_": ("settings A/B on unseen data", "2025-01..2025-08", "2025-09..2026-05"),
 "goldcand_": ("Gold candidates on unseen data", "2025-01..2025-08", "2025-09..2026-05"),
 "goldyears_": ("Gold G1/G2 on 2020-2024", "2020-01..2022-06", "2022-07..2024-12"),
 "expiry_": ("zone expiry test", "2026-06-01..2026-10-31", "2026-11-01..2026-11-20"),
 "gpt_r1/": ("ChatGPT round-1 ideas on G1 and H4+base", "see folder name", "see folder name"),
 "e1_1r/": ("Entry 1 only with 1R broker stop", "see folder name", "see folder name"),
 "next/": ("agreed tests: G1 buys only, Entry2 x stop grid, expiry, weekend cancel, RSI/ATR fill filters", "see folder name", "see folder name"),
 "recent_tr_": ("trader entries: sweep reclaim, rejection candle, break of structure", "2026-06-01..2026-10-31", "2026-11-01..2026-11-20"),
 "trval/": ("trader-entry candidates on unseen data", "see folder name", "see folder name"),
 "g1var": ("G1 Entry-1-only and expiry variants, continuous", "2020-01..2023-06", "2023-07..2026-11"),
 "persym_ustec": ("USTEC deep search (150 settings)", "see stage", "see stage"),
 "persym_m2/": ("M2 per-symbol search", "see stage", "see stage"),
 "persym_all/": ("all-symbol deep search", "see stage", "see stage"),
 "regime_2026/": ("2026 regime-aware search (bull/bear by H4 trend)", "2026-01-01..2026-07-31", "2026-08-01..2026-11-20"),
 "persym/s1_": ("per-symbol search stage 1", "2026-06-01..2026-10-31", "2026-11-01..2026-11-20"),
 "persym/s2_": ("per-symbol stage 2 (unseen)", "2025-01..2025-08", "2025-09..2026-05"),
 "persym/s3_": ("per-symbol stage 3 (unseen)", "older half A", "older half B"),
}
rows = []
for f in sorted(glob.glob(f"{P}/**/split_runs.csv", recursive=True)):
    test = os.path.relpath(os.path.dirname(f), P).replace("\\", "/")
    meta = next((v for k, v in META.items() if test.startswith(k)), (test, "", ""))
    t = pd.read_csv(f)
    t.insert(0, "test", test); t.insert(1, "test_description", meta[0]); t.insert(2, "period_A", meta[1]); t.insert(3, "period_B", meta[2])
    rows.append(t)
all_ = pd.concat(rows, ignore_index=True)
all_ = all_.rename(columns={"tune_trades": "A_trades", "tune_win_rate": "A_win_pct", "tune_net_r": "A_net_r", "check_trades": "B_trades", "check_win_rate": "B_win_pct", "check_net_r": "B_net_r", "overrides": "settings_changed_from_default"})
cols = ["test", "test_description", "symbol", "timeframe", "period_A", "A_trades", "A_win_pct", "A_net_r", "period_B", "B_trades", "B_win_pct", "B_net_r", "bull_trades", "bull_net_r", "bear_trades", "bear_net_r", "settings_changed_from_default"]
all_[[c for c in cols if c in all_.columns]].to_csv(f"{OUT}/all_results.csv", index=False)
from services.indicator_lab.parameter_registry import default_parameters
json.dump(default_parameters("LEBPRIME"), open(f"{SET}/default_parameters.json", "w"), indent=1)
for f in ["rules_gold_final_M5.json"]:
    if os.path.exists(f"{P}/{f}"):
        json.dump(json.load(open(f"{P}/{f}")), open(f"{SET}/candidate_settings_gold_M5.json", "w"), indent=1)
for src, dst in (("persym", "per_symbol_pass_fail.csv"), ("persym_ustec", "ustec_deep_pass_fail.csv"), ("persym_ustec_m2", "ustec_M2_pass_fail.csv"), ("persym_m2", "M2_pass_fail.csv")):
    if os.path.exists(f"{P}/{src}/final_pass.csv") and os.path.getsize(f"{P}/{src}/final_pass.csv") > 5:
        pd.read_csv(f"{P}/{src}/final_pass.csv").to_csv(f"{OUT}/{dst}", index=False)
import shutil
for f in glob.glob(f"{P}/rules_*.json"):
    os.makedirs(f"{SET}/tested_rule_sets", exist_ok=True); shutil.copy(f, f"{SET}/tested_rule_sets/")
print(len(all_), "rows")
