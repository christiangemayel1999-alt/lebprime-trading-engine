"""Robustness grid for the New York opening-range breakout: range length, target, trend filter, holding time."""
import sys, os, itertools
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from mtf import frame, cost_r, manage, DS, START, MINUTES


def orb(sym, ltf, mins, tp, trend, hold_h, city="NY"):
    from zoneinfo import ZoneInfo
    lf = frame(sym, ltf, START.get(ltf, "2020-01-01"))
    rule = {"H1": "1h", "H4": "4h"}.get(trend)
    up = None
    if rule:
        h = lf.set_index("t").close.resample(rule).last().dropna()
        up = (h.ewm(span=50, adjust=False).mean().diff() > 0)
    lo, hi, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "close", "spread"))
    step = MINUTES[ltf]
    tz, hh, mm = {"NY": ("America/New_York", 9, 30), "London": ("Europe/London", 8, 0)}[city]
    loc = lf.t.dt.tz_convert(ZoneInfo(tz))
    opens = np.flatnonzero(((loc.dt.hour == hh) & (loc.dt.minute == mm)).to_numpy())
    rows = []
    for i0 in opens:
        n = max(1, mins // step)
        if i0 + n + 1 >= len(cl):
            continue
        rh, rl = hi[i0:i0 + n].max(), lo[i0:i0 + n].min()
        if rh <= rl:
            continue
        tu = None
        if up is not None:
            prior = up[up.index < lf.t[i0]]
            tu = bool(prior.iloc[-1]) if len(prior) else None
        for i in range(i0 + n, min(len(cl), i0 + n + 120 // step)):
            buy, sell = cl[i] > rh, cl[i] < rl
            if not (buy or sell):
                continue
            if (buy and tu is False) or (sell and tu is True):
                break
            entry = cl[i]
            stop = rl if buy else rh
            risk = abs(entry - stop)
            tgt = entry + tp * risk if buy else entry - tp * risk
            r = manage(lo, hi, cl, i, entry, stop, tgt, buy, hold_h * 60 // step)
            rows.append({"symbol": sym, "tf": ltf, "range_min": mins, "tp": tp, "trend": trend or "none", "hold_h": hold_h,
                         "side": "BUY" if buy else "SELL", "year": int(lf.t[i].year), "net_r": r - cost_r(sym, sp[i], risk)})
            break
    return rows


def run(job):
    return orb(*job)


if __name__ == "__main__":
    jobs = []
    for sym in ["US30", "USTEC", "XAUUSD", "XAGUSD", "WTI", "EURUSD"]:
        for ltf in ("M1", "M3", "M5"):
            if (sym, ltf) not in DS:
                continue
            for mins, tp, trend, hold in itertools.product((5, 15, 30), (1.0, 1.5, 2.0), ("H1", "H4", None), (2, 4)):
                jobs.append((sym, ltf, mins, tp, trend, hold))
    out = []
    with ProcessPoolExecutor(7) as p:
        for rows in p.map(run, jobs, chunksize=2):
            out += rows
    t = pd.DataFrame(out)
    t.to_csv("reports/phase4/orb_grid_trades.csv", index=False)
    summ = []
    for key, g in t.groupby(["symbol", "tf", "range_min", "tp", "trend", "hold_h"]):
        yr = g.groupby("year").net_r.sum()
        full = yr[[y for y in yr.index if y >= {"M1": 2023, "M3": 2021}.get(key[1], 2020)]]
        summ.append(dict(zip(["symbol", "tf", "range_min", "tp", "trend", "hold_h"], key), trades=len(g), win=round((g.net_r > 0).mean() * 100, 1),
                         avg_win=round(g[g.net_r > 0].net_r.mean(), 2), avg_loss=round(g[g.net_r <= 0].net_r.mean(), 2), net_r=round(g.net_r.sum(), 1),
                         losing_years=int((full <= 0).sum()), **{str(y): round(v, 1) for y, v in yr.items()}))
    s = pd.DataFrame(summ).sort_values("net_r", ascending=False)
    s.to_csv("LebPrime AI Research/02 - Backtest Results/orb_grid.csv", index=False)
    print(s.groupby(["symbol", "tf"]).apply(lambda g: pd.Series({"variants": len(g), "positive": int((g.net_r > 0).sum()), "no_losing_year": int((g.losing_years == 0).sum()), "best": g.net_r.max(), "median": g.net_r.median()}), include_groups=False).to_string())
    print(s.head(25).to_string(index=False))
    print("ALL DONE")
