"""New entry models: (A) higher-timeframe LebPrime zones with a lower-timeframe trigger; (B) London/NY opening-range breakout.
Every trade: entry at a candle close, broker stop and target (stop first if both in one candle), spread + commission. Per year 2020-2026."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd

DS = {}
for i, s in enumerate(["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]):
    DS[(s, "H4")] = 157 + i; DS[(s, "H1")] = 52 + i; DS[(s, "M30")] = 58 + i; DS[(s, "M15")] = 124 + i
    DS[(s, "M10")] = 163 + i; DS[(s, "M5")] = 130 + i; DS[(s, "M3")] = 136 + i
for i, s in enumerate(["XAUUSD", "XAGUSD", "US30", "USTEC"]):
    DS[(s, "M1")] = 148 + i; DS[(s, "M2")] = 142 + i
START = {"M1": "2023-01-01", "M2": "2022-01-01", "M3": "2021-01-01"}
ZONE_SET = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0, "trend_filter": "With H4 trend",
            "require_compact_base": True, "limit_expiry_bars": 2000}
MINUTES = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M10": 10, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


def frame(sym, tf, start):
    from phase4_search import _frame
    _, f = _frame(DS[(sym, tf)], f"{start}..2026-11-22")
    f = f.copy()
    f["t"] = pd.to_datetime(f.timestamp_utc, utc=True)
    return f.reset_index(drop=True)


def cost_r(sym, spread_pts, risk):
    from phase4_search import POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    if risk <= 0:
        return 0.0
    return spread_pts * POINT.get(sym, 0) / risk + COMMISSION_PER_LOT.get(sym, 0) / (risk * VALUE_PER_PRICE_PER_LOT.get(sym, 1))


def manage(lo, hi, cl, i0, entry, stop, tgt, buy, maxbars):
    risk = abs(entry - stop)
    for j in range(i0 + 1, min(len(cl), i0 + 1 + maxbars)):
        if (lo[j] <= stop) if buy else (hi[j] >= stop):
            return -1.0
        if (hi[j] >= tgt) if buy else (lo[j] <= tgt):
            return abs(tgt - entry) / risk
    j = min(len(cl) - 1, i0 + maxbars)
    return ((cl[j] - entry) if buy else (entry - cl[j])) / risk


def mtf_job(sym, htf, ltf, trig, tp):
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    start = START.get(ltf, "2020-01-01")
    hf = frame(sym, htf, start)
    lf = frame(sym, ltf, start)
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ZONE_SET}, symbol=sym, timeframe=htf).run(hf)
    lo, hi, op, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "open", "close", "spread"))
    prev = np.r_[cl[0], cl[:-1]]
    tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev), np.abs(lo - prev)))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()
    lt = lf.t.dt.tz_convert(None).to_numpy()
    rows = []
    for z in res["zones"]:
        if z.limit_created_time is None:
            continue
        buy = z.side == "DEMAND"
        top, bot = float(z.zone_top), float(z.zone_bottom)
        created = np.datetime64(pd.Timestamp(z.limit_created_time).tz_convert(None))
        # the HTF candle that created the LIMIT closes one HTF period after its open
        i = int(np.searchsorted(lt, created + np.timedelta64(MINUTES[htf], "m"), side="left"))
        end = min(len(cl), i + 3000)
        touched = None
        ext = swing = None
        while i < end:
            if touched is None:
                if (lo[i] <= top) if buy else (hi[i] >= bot):
                    touched = i
                    ext = lo[i] if buy else hi[i]
                    swing = hi[i] if buy else lo[i]
                if (cl[i] < bot) if buy else (cl[i] > top):
                    break
                i += 1
                continue
            if (cl[i] < bot) if buy else (cl[i] > top):
                break
            if i - touched > 60:
                break
            ok = False
            if trig == "bos" and i - touched >= 2:
                ok = (cl[i] > swing) if buy else (cl[i] < swing)
            if trig == "rejection":
                rng = hi[i] - lo[i]
                wick = (min(op[i], cl[i]) - lo[i]) if buy else (hi[i] - max(op[i], cl[i]))
                ok = rng > 0 and wick >= 0.5 * rng and ((cl[i] > op[i]) if buy else (cl[i] < op[i]))
            ext = min(ext, lo[i]) if buy else max(ext, hi[i])
            if ok:
                entry = cl[i]
                stop = ext - 0.1 * atr[i] if buy else ext + 0.1 * atr[i]
                if abs(entry - stop) < 0.25 * atr[i]:
                    stop = entry - 0.25 * atr[i] if buy else entry + 0.25 * atr[i]
                risk = abs(entry - stop)
                tgt = entry + tp * risk if buy else entry - tp * risk
                r = manage(lo, hi, cl, i, entry, stop, tgt, buy, 600)
                rows.append({"model": f"{ltf} {trig} in {htf} zones, TP {tp}R", "symbol": sym, "side": "BUY" if buy else "SELL",
                             "year": str(lf.t[i].year), "net_r": r - cost_r(sym, sp[i], risk)})
                break
            swing = max(swing, hi[i]) if buy else min(swing, lo[i])
            i += 1
    return rows


def orb_job(sym, ltf, tp, mins):
    from zoneinfo import ZoneInfo
    lf = frame(sym, ltf, START.get(ltf, "2020-01-01"))
    h1 = lf.set_index("t").close.resample("1h").last().dropna()
    up = (h1.ewm(span=50, adjust=False).mean().diff() > 0)
    lo, hi, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "close", "spread"))
    step = MINUTES[ltf]
    rows = []
    for city, tz, hh, mm in (("London", "Europe/London", 8, 0), ("NY", "America/New_York", 9, 30)):
        loc = lf.t.dt.tz_convert(ZoneInfo(tz))
        opens = np.flatnonzero(((loc.dt.hour == hh) & (loc.dt.minute == mm)).to_numpy())
        for i0 in opens:
            n = max(1, mins // step)
            if i0 + n + 1 >= len(cl):
                continue
            rh, rl = hi[i0:i0 + n].max(), lo[i0:i0 + n].min()
            if rh <= rl:
                continue
            prior = up[up.index < lf.t[i0]]
            trend_up = bool(prior.iloc[-1]) if len(prior) else None
            for i in range(i0 + n, min(len(cl), i0 + n + 120 // step)):
                buy, sell = cl[i] > rh, cl[i] < rl
                if not (buy or sell):
                    continue
                if (buy and trend_up is False) or (sell and trend_up is True):
                    break
                entry = cl[i]
                stop = rl if buy else rh
                risk = abs(entry - stop)
                tgt = entry + tp * risk if buy else entry - tp * risk
                r = manage(lo, hi, cl, i, entry, stop, tgt, buy, 240 // step)
                rows.append({"model": f"{ltf} {city} opening-range breakout ({mins} min), TP {tp}R, H1 trend", "symbol": sym,
                             "side": "BUY" if buy else "SELL", "year": str(lf.t[i].year), "net_r": r - cost_r(sym, sp[i], risk)})
                break
    return rows


def run(job):
    return mtf_job(*job[1:]) if job[0] == "mtf" else orb_job(*job[1:])


if __name__ == "__main__":
    pairs = [("M5", "M1"), ("M15", "M1"), ("M15", "M3"), ("H1", "M3"), ("M15", "M2"), ("H1", "M10"), ("H4", "M30"), ("H4", "H1")]
    jobs = []
    for sym in ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]:
        for htf, ltf in pairs:
            if (sym, ltf) in DS and (sym, htf) in DS:
                for trig in ("bos", "rejection"):
                    for tp in (2.0, 3.0):
                        jobs.append(("mtf", sym, htf, ltf, trig, tp))
        for ltf in ("M1", "M3", "M5"):
            if (sym, ltf) in DS:
                for tp in (1.5, 2.0):
                    jobs.append(("orb", sym, ltf, tp, 15))
    out = []
    with ProcessPoolExecutor(7) as p:
        for rows in p.map(run, jobs):
            out += rows
            pd.DataFrame(out).to_csv("reports/phase4/mtf_trades.csv", index=False)
    t = pd.DataFrame(out)
    summ = []
    for (m, s), g in t.groupby(["model", "symbol"]):
        yr = g.groupby("year").net_r.sum()
        summ.append({"model": m, "symbol": s, "trades": len(g), "win": round((g.net_r > 0).mean() * 100, 1),
                     "avg_win": round(g[g.net_r > 0].net_r.mean(), 2), "avg_loss": round(g[g.net_r <= 0].net_r.mean(), 2),
                     "net_r": round(g.net_r.sum(), 1), "years": len(yr), "losing_years": int((yr <= 0).sum()),
                     **{y: round(yr.get(y, 0.0), 1) for y in [str(x) for x in range(2020, 2027)]}})
    s = pd.DataFrame(summ).sort_values("net_r", ascending=False)
    s.to_csv("LebPrime AI Research/02 - Backtest Results/new_models_by_year.csv", index=False)
    print(s.head(40).to_string(index=False))
    print("ALL DONE")
