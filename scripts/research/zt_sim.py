"""Zone parity study, step 2: Christian's trade on every TradingView zone, with a break-even + trailing exit.

Order: one BUY/SELL LIMIT at Entry 1 (near zone edge), placed when the zone's LIMIT CREATED candle closes.
Broker SL = the zone's Pine SL (= 1R), touch-triggered. Buys fill on ask and exit on bid, sells the reverse,
real M1 spread, commission per lot; no slippage. The unfilled limit is cancelled when a TF candle closes beyond
the zone's far edge (Pine/bot rule) or when the zone leaves the 500-candle chart window.
Exit: break-even after +BE R (stop to Entry 1 + lock), trailing after +START R, no take profit.
The stop only moves at a TF candle close (the bot manages on candle closes) using data up to that close,
or every minute when UPD=1, and the new stop only applies from the next minute: no look-ahead."""
import sys, os, itertools
import numpy as np, pandas as pd
from numba import njit
ZD = "/var/lib/lebprime/reports/zonetrail/zones"; OUT = "/var/lib/lebprime/reports/zonetrail"
POINT = {"XAUUSD": 0.01, "XAGUSD": 0.001, "US30": 0.1, "USTEC": 0.1, "WTI": 0.001, "EURUSD": 0.00001}
COMM = {"XAUUSD": 7.0, "XAGUSD": 7.0, "US30": 0.70, "USTEC": 0.70, "WTI": 7.0, "EURUSD": 7.0}
VAL = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0, "USTEC": 1.0, "WTI": 1000.0, "EURUSD": 100000.0}
EP = pd.Timestamp("1970-01-01", tz="UTC")
WIN = 500


def mins(s): return ((pd.to_datetime(s, utc=True) - EP) // pd.Timedelta(minutes=1)).to_numpy(np.int64)


_M = {}
def load(sym):
    if sym in _M: return _M[sym]
    cache = f"{OUT}/m1_{sym}.npz"
    if os.path.exists(cache):
        z = np.load(cache); m = {k: z[k] for k in z.files}
    else:
        d = pd.read_csv(f"/var/lib/lebprime/mt5_data/M1/{sym}.csv", usecols=["timestamp_utc", "open", "high", "low", "close", "spread"])
        d = d[d.timestamp_utc >= "2019-12-01"]
        m = {"t": mins(d.timestamp_utc), "o": d.open.to_numpy(float), "h": d.high.to_numpy(float), "l": d.low.to_numpy(float),
             "c": d.close.to_numpy(float), "sp": np.maximum(d.spread.to_numpy(float), 3) * POINT[sym]}
        np.savez(cache, **m)
    _M[sym] = m
    return m


def tf_arrays(m, k):
    g = m["t"] // k
    end = np.r_[g[1:] != g[:-1], True]                      # last minute of each TF candle
    tfi = np.cumsum(np.r_[True, g[1:] != g[:-1]]) - 1        # TF candle index of every minute
    s = pd.DataFrame({"g": g, "h": m["h"], "l": m["l"], "c": m["c"], "sp": m["sp"]}).groupby("g", sort=True)
    H = s.h.max().to_numpy(); L = s.l.min().to_numpy(); C = s.c.last().to_numpy(); SPL = s.sp.last().to_numpy()
    start_min = s.g.first().to_numpy() * k if False else np.unique(g) * k
    pc = np.r_[C[0], C[:-1]]; tr = np.maximum(H - L, np.maximum(np.abs(H - pc), np.abs(L - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    return {"end": end, "tfi": tfi.astype(np.int64), "H": H, "L": L, "C": C, "ATR": atr, "start": start_min.astype(np.int64)}


@njit(cache=True)
def sim(o, h, l, c, sp, end, tfi, TH, TL, TATR, side, E, SL, top, bot, j0, jexp, comm, be_r, lock_cost, lock_r, tstart, tmode, tval, upd_min, tp_r):
    n = len(side); N = len(o)
    res = np.full((n, 9), np.nan)   # fidx, fill, xidx, exit, reason, risk, grossR, netR, mfeR
    for k in range(n):
        s = side[k]; e = E[k]; sl = SL[k]
        fj = -1; fpx = 0.0
        je = min(jexp[k], N - 1)
        for j in range(j0[k], je + 1):
            if s > 0 and l[j] + sp[j] <= e: fj = j; fpx = min(e, o[j] + sp[j]); break
            if s < 0 and h[j] >= e: fj = j; fpx = max(e, o[j]); break
            if end[j] and ((s > 0 and c[j] < bot[k]) or (s < 0 and c[j] > top[k])): break     # close beyond the far edge: cancel
        if fj < 0: res[k, 4] = 0; continue
        risk = (fpx - sl) * s
        res[k, 0] = fj; res[k, 1] = fpx; res[k, 5] = risk
        if risk <= 0: res[k, 4] = -1; continue
        tp = fpx + s * tp_r * risk if tp_r > 0 else 0.0
        cur = sl; best = fpx; xj = -1; xpx = 0.0; reason = 0; mfe = 0.0
        lock = lock_r * risk + (sp[fj] + comm if lock_cost else 0.0)
        for j in range(fj, N):
            if s > 0:
                if l[j] <= cur:
                    xpx = cur if j == fj else min(cur, o[j]); xj = j; break
                if tp_r > 0 and j > fj and h[j] >= tp:
                    xpx = max(tp, o[j]); xj = j; reason = 1; break
                fav = c[j] if j == fj else h[j]
                if fav > best: best = fav
            else:
                if h[j] + sp[j] >= cur:
                    xpx = cur if j == fj else max(cur, o[j] + sp[j]); xj = j; break
                if tp_r > 0 and j > fj and l[j] + sp[j] <= tp:
                    xpx = min(tp, o[j] + sp[j]); xj = j; reason = 1; break
                fav = c[j] + sp[j] if j == fj else l[j] + sp[j]
                if fav < best: best = fav
            m_r = (best - fpx) * s / risk
            if m_r > mfe: mfe = m_r
            if upd_min or end[j]:
                new = cur
                if be_r > 0 and mfe >= be_r:
                    b = fpx + s * lock
                    if (b - new) * s > 0: new = b
                if tmode > 0 and mfe >= tstart:
                    ti = tfi[j]
                    if tmode == 1: cand = best - s * tval * risk
                    elif tmode == 2: cand = best - s * tval * TATR[ti]
                    else:
                        nb = int(tval); lo = ti - nb + 1
                        if lo < 0: lo = 0
                        if s > 0:
                            cand = TL[lo]
                            for q in range(lo, ti + 1):
                                if TL[q] < cand: cand = TL[q]
                        else:
                            cand = TH[lo]
                            for q in range(lo, ti + 1):
                                if TH[q] > cand: cand = TH[q]
                            cand = cand + sp[j]
                    # a long stop must stay below the current bid (sell: above the ask)
                    if s > 0 and cand >= c[j]: cand = -1e18
                    if s < 0 and cand <= c[j] + sp[j]: cand = 1e18
                    if (cand - new) * s > 0: new = cand
                cur = new
        if xj < 0:
            xj = N - 1; xpx = c[N - 1] if s > 0 else c[N - 1] + sp[N - 1]; reason = 4
        if reason == 0:
            reason = 2 if (cur - sl) * s <= 0 else (3 if (xpx - fpx) * s <= 0.25 * risk else 5)
        g = (xpx - fpx) * s / risk
        res[k, 2] = xj; res[k, 3] = xpx; res[k, 4] = reason; res[k, 6] = g; res[k, 7] = g - comm / risk; res[k, 8] = mfe
    return res


REASON = {0: "not_filled", -1: "invalid", 1: "TP", 2: "SL", 3: "BE", 4: "open_at_end", 5: "trail"}
_PREP = {}
def prep(sym, tf):
    key = (sym, tf)
    if key in _PREP: return _PREP[key]
    k = int(tf[1:]); m = load(sym); T = tf_arrays(m, k)
    z = pd.read_pickle(f"{ZD}/{sym}_{tf}_w{WIN}.pkl")
    side = np.where(z.side == "DEMAND", 1, -1).astype(np.int64)
    j0 = np.searchsorted(m["t"], mins(z.limit_time) + k)               # first minute after the LIMIT CREATED candle closed
    ci = np.searchsorted(T["start"], mins(z.candidate_time))
    exp_tf = np.minimum(ci + WIN, len(T["start"]) - 1)
    jexp = np.searchsorted(m["t"], T["start"][exp_tf]) - 1             # zone leaves the 500-candle window
    jexp[ci + WIN >= len(T["start"])] = len(m["t"]) - 1
    _PREP[key] = (m, T, z, side, j0.astype(np.int64), jexp.astype(np.int64))
    return _PREP[key]


def run(sym, tf, be_r=0.5, lock_cost=1, lock_r=0.0, tstart=1.0, tmode=1, tval=1.0, upd_min=0, tp_r=0.0):
    m, T, z, side, j0, jexp = prep(sym, tf)
    comm = COMM[sym] / VAL[sym]
    r = sim(m["o"], m["h"], m["l"], m["c"], m["sp"], T["end"], T["tfi"], T["H"], T["L"], T["ATR"], side,
            z.entry1.to_numpy(float), z.sl.to_numpy(float), z.top.to_numpy(float), z.bottom.to_numpy(float), j0, jexp,
            comm, be_r, lock_cost, lock_r, tstart, tmode, tval, upd_min, tp_r)
    d = pd.DataFrame(r, columns=["fidx", "fill", "xidx", "exit", "reason", "risk", "gross_r", "net_r", "mfe_r"])
    d["symbol"] = sym; d["tf"] = tf; d["side"] = np.where(side > 0, "BUY", "SELL"); d["zone_id"] = z.zone_id.to_numpy()
    d["entry1"] = z.entry1.to_numpy(); d["sl"] = z.sl.to_numpy(); d["limit_time"] = z.limit_time.to_numpy()
    d = d[d.reason > 0].copy()
    f = d.fidx.to_numpy(np.int64); x = d.xidx.to_numpy(np.int64)
    d["fill_time"] = pd.to_datetime(m["t"][f], unit="m", utc=True); d["exit_time"] = pd.to_datetime(m["t"][x], unit="m", utc=True)
    d["year"] = d.fill_time.dt.year; d["hold_min"] = m["t"][x] - m["t"][f]; d["exit_reason"] = d.reason.map(REASON)
    return d
