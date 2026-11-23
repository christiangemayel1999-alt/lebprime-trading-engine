"""Per-symbol search: tune Jun-Oct 2026 / check Nov 2026, then require profit on Jan 2025-May 2026 and on 2020-2024.
Resumable (each stage skips finished output). Writes reports/phase4/persym/final_pass.csv."""
import json, random, subprocess, sys, os
from pathlib import Path
import pandas as pd
OUT = Path("reports/phase4/persym_ustec"); OUT.mkdir(parents=True, exist_ok=True)
TFS = {"M15": [127], "M5": [133], "M3": [139], "M1": [151]}
OLD = {"M15": "2020-01-01..2025-01-01", "M5": "2020-01-01..2025-01-01", "M3": "2021-01-01..2025-01-01", "M1": "2023-01-01..2025-01-01"}
OLD_SPLIT = {"M15": "2022-07-01", "M5": "2022-07-01", "M3": "2023-01-01", "M1": "2024-01-01"}
FIX = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0}
SP = {"entry_mode_x": 1, "trend_filter": ["With H4 trend", "With H4 trend", "With EMA200", "None"], "momentum_count": [2, 3, 4], "body_mult": [0.4, 0.5, 0.7],
      "min_zone_width_atr": [0.1, 0.2, 0.3], "zone_refinement": ["Smallest Range Candle", "Full Base (Boundary 9)"], "entry_mode": ["Limit", "Close confirmation", "Sweep reclaim", "Rejection candle", "Break of structure"], "side_filter": ["Both", "Buys only", "Sells only"], "confirm_stop_buffer_atr": [0.1, 0.3],
      "tp1_r": [0.75, 1.0], "tp1_close_fraction": [0.5, 1.0], "tp2_r": [2.0, 3.0], "session_filter": ["All", "London + New York", "London only"],
      "block_beirut_window": ["Off", "15:00-17:00"], "max_risk_atr": [0.0, 2.0], "min_adx": [0, 20, 25], "require_compact_base": [True, False], "max_adx": [0, 40], "atr_pct_min": [0, 20],
      "atr_pct_max": [100, 80], "max_spread_risk": [0, 0.05, 0.1], "limit_expiry_bars": [0, 300, 1000]}

def split(settings_path, datasets, window, split_date, out):
    if (Path(out) / "split_pooled.csv").exists():
        return
    subprocess.run([sys.executable, "scripts/phase4_search.py", "split", "--window", window, "--split", split_date, "--settings", str(settings_path),
                    "--datasets", *map(str, datasets), "--out", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

rng = random.Random(21)
g1 = json.load(open("reports/phase4/rules_gold_final_M5.json"))
rules = []; seen = {json.dumps(r, sort_keys=True) for r in rules}
while len(rules) < 150:
    t = {**FIX, **{k: rng.choice(v) for k, v in SP.items() if k != "entry_mode_x"}}
    if t["entry_mode"] in ("Sweep reclaim", "Rejection candle", "Break of structure"):
        t["stop_rule"] = "Price touch"
    else:
        t.pop("confirm_stop_buffer_atr", None)
    k = json.dumps(t, sort_keys=True)
    if k not in seen:
        seen.add(k); rules.append(t)
json.dump(rules, open(OUT / "rules.json", "w"))
final = []
for tf, ds in TFS.items():
    s1 = OUT / f"s1_{tf}"
    split(OUT / "rules.json", ds, "2026-06-01..2026-11-21", "2026-11-01", s1)
    print(tf, "stage 1 done", flush=True)
    t = pd.read_csv(s1 / "split_runs.csv")
    cand = t[(t.tune_net_r > 0) & (t.check_net_r > 0) & (t.tune_trades >= 30)].sort_values("tune_net_r", ascending=False).groupby("dataset_id").head(8)
    for d, grp in cand.groupby("dataset_id"):
        sett = [json.loads(o) for o in grp.overrides]
        p = OUT / f"s2_{tf}_{d}.json"; json.dump(sett, open(p, "w"))
        split(p, [d], "2025-01-01..2026-06-01", "2025-09-01", OUT / f"s2_{tf}_{d}")
        t2 = pd.read_csv(OUT / f"s2_{tf}_{d}" / "split_runs.csv")
        ok = t2[(t2.tune_net_r > 0) & (t2.check_net_r > 0)]
        if ok.empty:
            continue
        p3 = OUT / f"s3_{tf}_{d}.json"; json.dump([json.loads(o) for o in ok.overrides], open(p3, "w"))
        split(p3, [d], OLD[tf], OLD_SPLIT[tf], OUT / f"s3_{tf}_{d}")
        t3 = pd.read_csv(OUT / f"s3_{tf}_{d}" / "split_runs.csv")
        for _, r3 in t3.iterrows():
            r1 = grp[grp.overrides == r3.overrides].iloc[0]; r2 = ok[ok.overrides == r3.overrides].iloc[0]
            final.append({"tf": tf, "symbol": r3.symbol, "recent_tune": r1.tune_net_r, "recent_tune_n": r1.tune_trades, "nov": r1.check_net_r,
                          "y2025a": r2.tune_net_r, "y2025b_26": r2.check_net_r, "n2025": r2.tune_trades + r2.check_trades,
                          "old_a": r3.tune_net_r, "old_b": r3.check_net_r, "n_old": r3.tune_trades + r3.check_trades,
                          "passes_all": bool(r3.tune_net_r > 0 and r3.check_net_r > 0), "overrides": r3.overrides})
        pd.DataFrame(final).to_csv(OUT / "final_pass.csv", index=False)
    print(tf, "done", flush=True)
pd.DataFrame(final).to_csv(OUT / "final_pass.csv", index=False)
print("ALL DONE", flush=True)
