"""NY opening-range breakout picks: fixed TP vs trailing the stop once the trade reaches +1R."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from mtf import frame, cost_r, START, MINUTES


def exit_r(lo, hi, cl, i0, entry, stop, buy, maxbars, mode, tp):
    """mode: fixed | lock05_trail1 | lock05_trail05 | candle | half_trail1. Returns R (whole position)."""
    risk = abs(entry - stop); sgn = 1 if buy else -1
    best = 0.0; cur_stop = stop; half_done = False; banked = 0.0
    for j in range(i0 + 1, min(len(cl), i0 + 1 + maxbars)):
        adverse = (lo[j] - entry) * sgn / risk if buy else (entry - hi[j]) / risk * -1 * -1
        low_r = ((lo[j] - entry) / risk) if buy else ((entry - hi[j]) / risk)
        high_r = ((hi[j] - entry) / risk) if buy else ((entry - lo[j]) / risk)
        stop_r = (cur_stop - entry) / risk * sgn
        if low_r <= stop_r:
            rem = 0.5 if half_done else 1.0
            return banked + rem * stop_r
        if mode == "fixed":
            if high_r >= tp:
                return tp
            continue
        if mode == "half_trail1" and not half_done and high_r >= 1.0:
            half_done = True; banked = 0.5 * 1.0
        best = max(best, high_r)
        if best >= 1.0:
            if mode in ("lock05_trail1", "half_trail1"):
                new = max(0.5, best - 1.0)
            elif mode == "lock05_trail05":
                new = max(0.5, best - 0.5)
            elif mode == "candle":
                prev_low = ((lo[j] - entry) / risk) if buy else ((entry - hi[j]) / risk)
                new = max(0.5, prev_low)
            else:
                new = stop_r
            if new > stop_r:
                cur_stop = entry + new * risk * sgn
    j = min(len(cl) - 1, i0 + maxbars)
    last = ((cl[j] - entry) if buy else (entry - cl[j])) / risk
    return banked + (0.5 if half_done else 1.0) * last


def job(args):
    sym, ltf, mins, tp, trend, hold_h = args
    from zoneinfo import ZoneInfo
    lf = frame(sym, ltf, START.get(ltf, "2020-01-01"))
    h = lf.set_index("t").close.resample({"H1": "1h", "H4": "4h"}[trend]).last().dropna()
    up = (h.ewm(span=50, adjust=False).mean().diff() > 0)
    lo, hi, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "close", "spread"))
    step = MINUTES[ltf]
    loc = lf.t.dt.tz_convert(ZoneInfo("America/New_York"))
    rows = []
    for i0 in np.flatnonzero(((loc.dt.hour == 9) & (loc.dt.minute == 30)).to_numpy()):
        n = max(1, mins // step)
        if i0 + n + 1 >= len(cl):
            continue
        rh, rl = hi[i0:i0 + n].max(), lo[i0:i0 + n].min()
        if rh <= rl:
            continue
        prior = up[up.index < lf.t[i0]]
        tu = bool(prior.iloc[-1]) if len(prior) else None
        for i in range(i0 + n, min(len(cl), i0 + n + 120 // step)):
            buy, sell = cl[i] > rh, cl[i] < rl
            if not (buy or sell):
                continue
            if (buy and tu is False) or (sell and tu is True):
                break
            entry = cl[i]; stop = rl if buy else rh; risk = abs(entry - stop)
            c = cost_r(sym, sp[i], risk)
            for mode in ("fixed", "lock05_trail1", "lock05_trail05", "candle", "half_trail1"):
                hold = hold_h * 60 // step if mode == "fixed" else 8 * 60 // step
                r = exit_r(lo, hi, cl, i, entry, stop, buy, hold, mode, tp)
                rows.append({"setup": f"{sym} {ltf} NY breakout", "mode": mode, "year": str(lf.t[i].year), "net_r": r - c})
            break
    return rows


if __name__ == "__main__":
    picks = [("US30", "M3", 15, 1.5, "H4", 4), ("US30", "M1", 15, 2.0, "H4", 4), ("USTEC", "M1", 5, 2.0, "H4", 4),
             ("XAUUSD", "M1", 5, 2.0, "H4", 4), ("XAUUSD", "M3", 15, 2.0, "H4", 4), ("XAUUSD", "M5", 15, 2.0, "H4", 2)]
    with ProcessPoolExecutor(6) as p:
        rows = [r for rs in p.map(job, picks) for r in rs]
    t = pd.DataFrame(rows)
    out = []
    for (s, m), g in t.groupby(["setup", "mode"]):
        yr = g.groupby("year").net_r.sum()
        out.append({"setup": s, "exit": m, "trades": len(g), "win": round((g.net_r > 0).mean() * 100, 1), "avg_win": round(g[g.net_r > 0].net_r.mean(), 2),
                    "avg_loss": round(g[g.net_r <= 0].net_r.mean(), 2), "net_r": round(g.net_r.sum(), 1), "losing_years": int((yr <= 0).sum()),
                    **{y: round(yr.get(y, 0.0), 1) for y in [str(x) for x in range(2020, 2027)]}})
    r = pd.DataFrame(out)
    r.to_csv("LebPrime AI Research/02 - Backtest Results/orb_trailing_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
