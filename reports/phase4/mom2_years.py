"""2020-2026, per symbol x timeframe (M5/M15/M30), original settings + fixed broker SL, momentum 4 vs 2, with and without the filters
(Entry 1 only, costs <= 10% of the stop, no 15-17 Beirut). Continuous ledgers with costs (portfolio.run)."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd, numpy as np
from portfolio import run as zone_run
SYMS = ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]; BASE = {"M5": 130, "M15": 124, "M30": 58}
BUF = {"gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0, "stop_rule": "Price touch"}
FILT = {"entry2_mode": "Off", "max_spread_risk": 0.1, "block_beirut_window": "15:00-17:00"}
V = {"mom4": {}, "mom2": {"momentum_count": 2}, "mom4+filters": FILT, "mom2+filters": {"momentum_count": 2, **FILT}}
if __name__ == "__main__":
    jobs = [(f"{v}|{s}|{tf}", s, tf, BASE[tf] + i, {**BUF, **ov}) for v, ov in V.items() for i, s in enumerate(SYMS) for tf in BASE]
    out = []; os.makedirs("reports/phase4/mom2", exist_ok=True)
    with ProcessPoolExecutor(int(os.environ.get("WORKERS", "3"))) as p:
        for j, rs in zip(jobs, p.map(zone_run, jobs)):
            t = pd.DataFrame(rs); v, s, tf = j[0].split("|")
            t.to_csv(f"reports/phase4/mom2/{v}_{s}_{tf}.csv", index=False)
            if not len(t): continue
            t = t.sort_values("exit_time"); yr = t.groupby(t.exit_time.astype(str).str[:4]).net_r.sum(); eq = np.r_[0, t.net_r.cumsum()]
            out.append({"variant": v, "symbol": s, "tf": tf, "trades": len(t), "win": round((t.net_r > 0).mean() * 100, 1), "avg_win": round(t[t.net_r > 0].net_r.mean(), 2),
                        "avg_loss": round(t[t.net_r <= 0].net_r.mean(), 2), "net_r": round(t.net_r.sum(), 1), "usd_at_2pct_of_1000": round(t.net_r.sum() * 20, 0),
                        "max_dd_r": round((np.maximum.accumulate(eq) - eq).max(), 1), "buys_r": round(t[t.side == "BUY"].net_r.sum(), 1), "sells_r": round(t[t.side == "SELL"].net_r.sum(), 1),
                        "losing_years": int((yr <= 0).sum()), **{y: round(yr.get(y, np.nan), 1) for y in map(str, range(2020, 2027))}})
            print("done", j[0], out[-1]["net_r"], flush=True)
    r = pd.DataFrame(out); r.to_csv("reports/phase4/mom2_by_year.csv", index=False); print(r.to_string(index=False)); print("ALL DONE")
