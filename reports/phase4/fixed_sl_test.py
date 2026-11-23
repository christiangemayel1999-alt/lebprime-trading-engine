"""Fixed broker SL at the zone stop (stop_rule 'Price touch') vs candle-close SL, on Gold M5 G1B and Silver M30, 2 entries, 2020-2026, costs."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from money_sim import all_jobs
from portfolio import run as zone_run
if __name__ == "__main__":
    base = {j[1]: j for j in all_jobs() if j[0] == "zone"}
    jobs = []
    for name in ("Gold M5 G1B", "Silver M30 uptrend buys"):
        _, lab, s, tf, ds, ov = base[name]
        for v, extra in (("candle-close SL (now)", {}), ("fixed broker SL at zone stop", {"stop_rule": "Price touch"}),
                         ("fixed SL, Entry 1 only", {"stop_rule": "Price touch", "entry2_mode": "Off"})):
            jobs.append((f"{name} | {v}", s, tf, ds, {**ov, **extra}))
    with ProcessPoolExecutor(6) as p:
        res = list(p.map(zone_run, jobs))
    rows = []; allt = []
    for j, rs in zip(jobs, res):
        t = pd.DataFrame(rs); t["year"] = t.exit_time.astype(str).str[:4]; allt.append(t.assign(setting=j[0], symbol=j[1]))
        yr = t.groupby("year").net_r.sum(); eq = pd.concat([pd.Series([0.0]), t.sort_values("exit_time").net_r.cumsum()])
        rows.append({"setting": j[0], "trades": len(t), "win": round((t.net_r > 0).mean() * 100, 1), "avg_win": round(t[t.net_r > 0].net_r.mean(), 2),
                     "avg_loss": round(t[t.net_r <= 0].net_r.mean(), 2), "net_r": round(t.net_r.sum(), 1), "stress_0.02": round((t.net_r - 0.02).sum(), 1),
                     "max_dd_r": round((eq.cummax() - eq).max(), 1), "buys_r": round(t[t.side == "BUY"].net_r.sum(), 1), "sells_r": round(t[t.side == "SELL"].net_r.sum(), 1),
                     "losing_years": int((yr <= 0).sum()), **{y: round(yr.get(y, 0), 1) for y in map(str, range(2020, 2027))}})
    r = pd.DataFrame(rows); r.to_csv("LebPrime AI Research/02 - Backtest Results/fixed_sl_by_year.csv", index=False)
    pd.concat(allt).to_csv("reports/phase4/fixed_sl_trades.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
