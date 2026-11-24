"""Zone parity study, step 4: tables for Christian's exit, the best variant (picked on 2020-23 only) and the old TP 1R."""
import sys, os
sys.path.insert(0, "/var/lib/lebprime/research/zonetrail/research")
import numpy as np, pandas as pd
import zt_sim as S
from zt_grid import CFG, SYMS
TFS = ["M1", "M2", "M3", "M5", "M15", "M30"]
g = pd.read_csv(f"{S.OUT}/grid.csv")
g = g[g.config != "SL+BE0.5 only"]           # never exits except at a stop: holds trades for months, not a trading exit
rank = g.groupby("config")[["is_r", "oos_r", "net_r"]].sum().sort_values("is_r", ascending=False)
best = rank.index[0] if rank.index[0] != "HIS: BE0.5, trail from 1R, 1R behind" else rank.index[1]
picks = {"HIS": "HIS: BE0.5, trail from 1R, 1R behind", "BEST": best, "OLD": "TP 1R (old)"}
out = [f"Exit ranking, all 6 symbols x {len(TFS)} TFs, picked on 2020-23 (IS), checked on 2024-26 (OOS):", rank.round(1).to_string(), ""]
allt = []
for tag, name in picks.items():
    for sym in SYMS:
        for tf in TFS:
            if not os.path.exists(f"{S.ZD}/{sym}_{tf}_w{S.WIN}.pkl"): continue
            d = S.run(sym, tf, **CFG[name]); d["tag"] = tag; d["config"] = name; allt.append(d)
T = pd.concat(allt); T.to_pickle(f"{S.OUT}/trades_report.pkl")


def tab(d, by):
    f = lambda x: pd.Series({"trades": len(x), "win_pct": 100 * (x.net_r > 0).mean(), "net_r": x.net_r.sum(),
                             "avg_win": x.net_r[x.net_r > 0].mean(), "avg_loss": x.net_r[x.net_r <= 0].mean(),
                             "be_exits": (x.exit_reason == "BE").sum(), "trail_exits": (x.exit_reason == "trail").sum()})
    return d.groupby(by).apply(f)


for tag, name in picks.items():
    d = T[T.tag == tag]
    out += [f"=== {tag}: {name} ===", "By symbol and TF:", tab(d, ["symbol", "tf"]).round(2).to_string(), "",
            "Year by year, per symbol (all TFs):", tab(d, ["symbol", "year"]).round(2).to_string(), "",
            "Buys vs sells:", tab(d, ["symbol", "side"]).round(2).to_string(), ""]
    tab(d, ["symbol", "tf", "year"]).round(3).to_csv(f"{S.OUT}/year_by_year_{tag}.csv")
open(f"{S.OUT}/report.txt", "w").write("\n".join(out))
print("REPORT DONE", picks)
