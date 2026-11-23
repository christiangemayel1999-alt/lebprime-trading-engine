"""NY opening-range breakout, corrected after ChatGPT's review (23 Nov 2026):
- trend filter uses only COMPLETED H1/H4 bars (a bar labelled L is usable from L + period), so no future close leaks in;
- trades before the dataset's validation start are dropped (no warm-up trades are scored);
- every trade has its real entry time, exit time and exit reason; years are by exit time;
- trailing is compared with the fixed target at the SAME holding limit.
Costs: bar spread at entry + commission (cost_r)."""
import sys, os, itertools
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from mtf import frame, cost_r, DS, START, MINUTES

B = "LebPrime AI Research/02 - Backtest Results"
YEARS = [str(y) for y in range(2020, 2027)]


def causal_trend(lf, rule_minutes):
    s = lf.set_index("t").close.resample(f"{rule_minutes}min").last().dropna()
    up = s.ewm(span=50, adjust=False).mean().diff() > 0
    up.index = up.index + pd.Timedelta(minutes=rule_minutes)   # available only once the bar has closed
    return up


def exit_path(lo, hi, cl, i0, entry, stop, tp, buy, maxbars, mode):
    """Returns (R, exit index, reason). Stop is checked before the target inside a candle (conservative).
    mode 'fixed': target tp R. mode 'trail': at +1R lock +0.5R, then trail 1R behind the best price; target tp R still applies."""
    risk = abs(entry - stop); sgn = 1 if buy else -1
    cur = -1.0; best = 0.0
    last = min(len(cl) - 1, i0 + maxbars)
    for j in range(i0 + 1, last + 1):
        low_r = ((lo[j] - entry) if buy else (entry - hi[j])) / risk
        high_r = ((hi[j] - entry) if buy else (entry - lo[j])) / risk
        if low_r <= cur:
            return cur, j, "stop" if cur < 0 else "trail stop"
        if high_r >= tp:
            return tp, j, "target"
        if mode == "trail":
            best = max(best, high_r)
            if best >= 1.0:
                cur = max(cur, 0.5, best - 1.0)
    return ((cl[last] - entry) if buy else (entry - cl[last])) / risk, last, "time"


def orb(sym, ltf, mins, tp, trend, hold_h, mode="fixed"):
    from zoneinfo import ZoneInfo
    start = START.get(ltf, "2020-01-01")
    lf = frame(sym, ltf, start)
    up = causal_trend(lf, {"H1": 60, "H4": 240}[trend]) if trend else None
    lo, hi, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "close", "spread"))
    step = MINUTES[ltf]; n = max(1, mins // step)
    loc = lf.t.dt.tz_convert(ZoneInfo("America/New_York"))
    valid_from = pd.Timestamp(start, tz="UTC")
    rows = []
    for i0 in np.flatnonzero(((loc.dt.hour == 9) & (loc.dt.minute == 30)).to_numpy()):
        if lf.t[i0] < valid_from or i0 + n + 1 >= len(cl):
            continue
        rh, rl = hi[i0:i0 + n].max(), lo[i0:i0 + n].min()
        if rh <= rl:
            continue
        tu = None
        if up is not None:
            prior = up[up.index <= lf.t[i0]]
            tu = bool(prior.iloc[-1]) if len(prior) else None
        for i in range(i0 + n, min(len(cl), i0 + n + 120 // step)):
            buy, sell = cl[i] > rh, cl[i] < rl
            if not (buy or sell):
                continue
            if (buy and tu is False) or (sell and tu is True):
                break
            entry = cl[i]; stop = rl if buy else rh; risk = abs(entry - stop)
            r, j, why = exit_path(lo, hi, cl, i, entry, stop, tp, buy, hold_h * 60 // step, mode)
            t_entry = lf.t[i] + pd.Timedelta(minutes=step)      # entry at the breakout candle's close
            t_exit = lf.t[j] + pd.Timedelta(minutes=step) if why == "time" else lf.t[j]
            rows.append({"setting": f"{sym} {ltf} NY ORB {mins}m TP{tp} {trend or 'none'} {hold_h}h {mode}", "symbol": sym, "tf": ltf, "range_min": mins,
                         "tp": tp, "trend": trend or "none", "hold_h": hold_h, "mode": mode, "side": "BUY" if buy else "SELL",
                         "entry_time": str(t_entry), "exit_time": str(t_exit), "exit_reason": why, "risk_price": risk,
                         "gross_r": r, "net_r": r - cost_r(sym, sp[i], risk)})
            break
    return rows


def run(job):
    return orb(*job)


def summarise(t, keys):
    out = []
    for key, g in t.groupby(keys):
        g = g.sort_values("exit_time"); yr = g.groupby(g.exit_time.str[:4]); s = yr.net_r.sum(); c = yr.size()
        eq = np.r_[0.0, g.net_r.cumsum().to_numpy()]; dd = (np.maximum.accumulate(eq) - eq).max()
        stress = g.net_r - 0.02
        out.append({**dict(zip(keys, key if isinstance(key, tuple) else (key,))), "trades": len(g), "win": round((g.net_r > 0).mean() * 100, 1),
                    "avg_win": round(g[g.net_r > 0].net_r.mean(), 2), "avg_loss": round(g[g.net_r <= 0].net_r.mean(), 2),
                    "net_r": round(g.net_r.sum(), 1), "net_r_stress_0.02": round(stress.sum(), 1), "max_dd_r": round(dd, 1),
                    "buys_r": round(g[g.side == "BUY"].net_r.sum(), 1), "sells_r": round(g[g.side == "SELL"].net_r.sum(), 1),
                    "losing_years": int((s <= 0).sum()), "first_exit": g.exit_time.min()[:10], "last_exit": g.exit_time.max()[:10],
                    **{y: round(s.get(y, np.nan), 1) for y in YEARS}, **{f"n_{y}": int(c.get(y, 0)) for y in YEARS}})
    return pd.DataFrame(out)


if __name__ == "__main__":
    grid = []
    for sym in ["US30", "USTEC", "XAUUSD", "XAGUSD", "WTI", "EURUSD"]:
        for ltf in ("M1", "M3", "M5"):
            if (sym, ltf) not in DS:
                continue
            for mins, tp, trend, hold in itertools.product((5, 15, 30), (1.0, 1.5, 2.0), ("H1", "H4", None), (2, 4)):
                grid.append((sym, ltf, mins, tp, trend, hold, "fixed"))
    picks = [("XAUUSD", "M3", 15, 2.0, "H4"), ("XAUUSD", "M5", 15, 2.0, "H4"), ("US30", "M3", 15, 1.5, "H4"), ("US30", "M3", 15, 2.0, "H4"), ("USTEC", "M5", 15, 1.5, "H4")]
    trail = [(s, tf, m, tp, tr, h, mode) for s, tf, m, tp, tr in picks for h in (2, 4, 8) for mode in ("fixed", "trail")]
    with ProcessPoolExecutor(7) as p:
        g_rows = [r for rs in p.map(run, grid, chunksize=2) for r in rs]
        t_rows = [r for rs in p.map(run, trail) for r in rs]
    g = pd.DataFrame(g_rows); g.to_csv("reports/phase4/orb_v2_grid_trades.csv", index=False)
    gs = summarise(g, ["symbol", "tf", "range_min", "tp", "trend", "hold_h"]).sort_values("net_r", ascending=False)
    gs.to_csv(f"{B}/orb_v2_grid.csv", index=False)
    tt = pd.DataFrame(t_rows); tt.to_csv(f"{B}/orb_v2_picks_trades.csv", index=False)
    ts = summarise(tt, ["symbol", "tf", "range_min", "tp", "trend", "hold_h", "mode"])
    ts.to_csv(f"{B}/orb_v2_picks_fixed_vs_trail.csv", index=False)
    print(gs.groupby(["symbol", "tf"]).apply(lambda x: pd.Series({"variants": len(x), "positive": int((x.net_r > 0).sum()), "no_losing_year": int((x.losing_years == 0).sum()), "best": x.net_r.max()}), include_groups=False).to_string())
    print(ts.drop(columns=[f"n_{y}" for y in YEARS]).to_string(index=False))
    print("ALL DONE")
