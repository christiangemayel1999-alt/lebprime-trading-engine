"""Momentum candle count 2..6 on each zone setup, year by year, after costs."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from money_sim import all_jobs
from portfolio import run as zone_run

if __name__ == "__main__":
    base = [j for j in all_jobs() if j[0] == "zone"]
    jobs, keys = [], []
    for _, label, sym, tf, ds, ov in base:
        cur = ov.get("momentum_count", 4)
        for m in (2, 3, 4, 5, 6):
            jobs.append((label, sym, tf, ds, {**ov, "momentum_count": m})); keys.append((label, m, m == cur))
    with ProcessPoolExecutor(7) as p:
        res = list(p.map(zone_run, jobs))
    out = []
    for (label, m, cur), rows in zip(keys, res):
        t = pd.DataFrame(rows); t["year"] = t.exit_time.astype(str).str[:4]
        yr = t.groupby("year").net_r.sum()
        out.append({"setup": label, "momentum": m, "current": cur, "trades": len(t), "win": round((t.net_r > 0).mean() * 100, 1),
                    "net_r": round(t.net_r.sum(), 1), "losing_years": int((yr <= 0).sum()), **{y: round(yr.get(y, 0.0), 1) for y in map(str, range(2020, 2027))}})
    r = pd.DataFrame(out)
    r.to_csv("LebPrime AI Research/02 - Backtest Results/momentum_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
