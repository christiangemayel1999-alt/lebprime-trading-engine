"""LIMIT at the zone vs waiting for a reaction and entering at market, on the 6 zone setups.
Stop stays at the zone stop; entry and targets are re-based on the real fill, so R is from the actual entry."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from money_sim import all_jobs
from portfolio import run as zone_run

MODES = ["Limit", "Close confirmation", "Rejection candle", "Break of structure", "Sweep reclaim"]

if __name__ == "__main__":
    base = [j for j in all_jobs() if j[0] == "zone"]
    jobs, keys = [], []
    for _, label, sym, tf, ds, ov in base:
        for m in MODES:
            o = {**ov, "entry_mode": m}
            if m != "Limit":
                o.update({"confirm_stop": "Retest extreme", "confirm_stop_buffer_atr": 2.0, "confirm_min_risk_atr": 0.0})  # stop = the tighter of the zone stop and 2 ATR past the retest (almost always the zone stop), re-based on fill
            jobs.append((label, sym, tf, ds, o)); keys.append((label, m))
    with ProcessPoolExecutor(7) as p:
        res = list(p.map(zone_run, jobs))
    out = []
    lim = {}
    for (label, m), rows in zip(keys, res):
        t = pd.DataFrame(rows)
        if m == "Limit":
            lim[label] = t
    for (label, m), rows in zip(keys, res):
        t = pd.DataFrame(rows); t["year"] = t.exit_time.astype(str).str[:4]
        yr = t.groupby("year").net_r.sum(); L = lim[label]
        lw = L[L.net_r > 0]
        missed = int((~lw.limit_time.isin(set(t.limit_time))).sum()) if m != "Limit" else 0
        out.append({"setup": label, "entry": m, "trades": len(t), "limit_winners_missed": missed, "win": round((t.net_r > 0).mean() * 100, 1),
                    "avg_win": round(t[t.net_r > 0].net_r.mean(), 2), "avg_loss": round(t[t.net_r <= 0].net_r.mean(), 2), "net_r": round(t.net_r.sum(), 1),
                    "losing_years": int((yr <= 0).sum()), **{y: round(yr.get(y, 0.0), 1) for y in map(str, range(2020, 2027))}})
    r = pd.DataFrame(out)
    r.to_csv("LebPrime AI Research/02 - Backtest Results/confirm_entry_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
