"""Time Machine: honest odds for one zone before it is traded.

Given a zone (symbol, timeframe, side, Entry 1, SL, the time its LIMIT exists) this module answers
"what happened to zones like this before, and what do recent price paths say?" in two ways:

(a) Analogs. The bot's own LebPrimeEngine is run over past candles (the zone "library", cached per
    symbol/timeframe). Every past zone is traded with Christian's execution: one LIMIT at Entry 1,
    stop at the zone SL, TP = Entry 1 +/- (1R + extra) where extra is $0.20 on gold and 2x the median
    spread elsewhere. Fills and exits are touch-triggered on M1 candles with the real candle spread
    (buys fill on the ask and exit on the bid; sells the other way round), a stop wins when both are hit
    in the same minute, plus the round-trip commission. Zones are then filtered to the ones that look like
    this zone: same side, similar SL size in ATR, same H1 trend, same session, same zone width.

(b) Monte Carlo. Recent M1 candles (before the zone's time) are resampled in blocks into thousands of
    3-day paths that start at the fill. It counts how often TP is touched before SL.

No look-ahead anywhere: every indicator is read from candles that had CLOSED when the LIMIT was placed,
analogs only count trades that had already EXITED by then, and the Monte Carlo only uses candles from
before that time. Nothing here talks to MT5, the live bot or its orders.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

SYMBOLS = ("XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD")
ALIASES = {"NAS100": "USTEC", "NAS": "USTEC", "GOLD": "XAUUSD", "XAU": "XAUUSD", "DJ30": "US30", "USOIL": "WTI"}
TIMEFRAMES = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M10": 10, "M15": 15, "M30": 30, "M45": 45, "H1": 60, "H4": 240}
# Same cost model as the strategy tests (scripts/phase4_search.py, measured on the demo account).
POINT = {"XAUUSD": 0.01, "XAGUSD": 0.001, "US30": 0.1, "USTEC": 0.1, "WTI": 0.001, "EURUSD": 0.00001}
COMMISSION_PER_LOT = {"XAUUSD": 7.0, "XAGUSD": 7.0, "US30": 0.70, "USTEC": 0.70, "WTI": 7.0, "EURUSD": 7.0}
VALUE_PER_PRICE_PER_LOT = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0, "USTEC": 1.0, "WTI": 1000.0, "EURUSD": 100000.0}
GOLD_TP_EXTRA = 0.20
MAX_HOLD_MIN = 3 * 1440          # a filled trade still open after 3 days is closed at the market
LIVE_WINDOW_BARS = 360            # the live bot only scans its last 360 candles, so older untouched LIMITs never fill there
SL_BAND = (0.8, 1.25)             # "similar SL": analog SL in ATR within -20%/+25% of this zone's
MIN_TRUST = 30                    # fewer analogs than this are shown but flagged as too few to trust
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
SESSIONS = ("Asia", "London", "NY open", "NY late", "Rollover")


def canonical_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    return ALIASES.get(s, s)


def to_min(ts: Any) -> np.ndarray:
    t = pd.to_datetime(pd.Series(np.atleast_1d(ts)), utc=True)
    return ((t - EPOCH) // pd.Timedelta(minutes=1)).to_numpy(np.int64)


# ------------------------------------------------------------------ data
@dataclass
class M1:
    t: np.ndarray   # minute since epoch (candle open)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    sp: np.ndarray  # spread in price units (bid candles; ask = bid + spread)
    tick_volume: np.ndarray

    def __len__(self) -> int:
        return len(self.t)

    def slice(self, start_min: int | None = None, end_min: int | None = None) -> "M1":
        a = 0 if start_min is None else int(np.searchsorted(self.t, start_min))
        b = len(self.t) if end_min is None else int(np.searchsorted(self.t, end_min))
        return M1(*(getattr(self, f)[a:b] for f in ("t", "o", "h", "l", "c", "sp", "tick_volume")))


def load_m1(path: Path, symbol: str, start: str | None = None, end: str | None = None) -> M1:
    """Closed M1 candles from a research-store CSV (timestamp_utc, open, high, low, close, tick_volume, spread)."""
    chunks = []
    for chunk in pd.read_csv(path, chunksize=500_000, dtype={"timestamp_utc": str},
                             usecols=["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread"]):
        ts = chunk["timestamp_utc"]
        keep = np.ones(len(chunk), bool)
        if start:
            keep &= (ts >= start[:19]).to_numpy()
        if end:
            keep &= (ts < end[:19]).to_numpy()
        if keep.any():
            chunks.append(chunk[keep])
    if not chunks:
        raise ValueError(f"{symbol}: no M1 candles in {path.name} between {start} and {end}")
    d = pd.concat(chunks, ignore_index=True).drop_duplicates("timestamp_utc").sort_values("timestamp_utc")
    point = POINT.get(symbol, 0.0)
    return M1(t=to_min(d["timestamp_utc"]), o=d["open"].to_numpy(float), h=d["high"].to_numpy(float), l=d["low"].to_numpy(float),
              c=d["close"].to_numpy(float), sp=np.maximum(d["spread"].to_numpy(float) * point, 3 * point),
              tick_volume=d["tick_volume"].to_numpy(float))


def resample(m: M1, minutes: int) -> pd.DataFrame:
    """Candles of `minutes` built from M1 (bucket = open time, aligned to UTC midnight), as the engine expects."""
    g = m.t // minutes
    d = pd.DataFrame({"g": g, "open": m.o, "high": m.h, "low": m.l, "close": m.c, "tick_volume": m.tick_volume, "spread": m.sp})
    out = d.groupby("g", sort=True).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                                        tick_volume=("tick_volume", "sum"), spread=("spread", "last"))
    out["start"] = out.index.to_numpy(np.int64) * minutes
    out["timestamp_utc"] = pd.to_datetime(out["start"], unit="m", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    out["real_volume"] = 0.0
    return out.reset_index(drop=True)


def engine_frame(m: M1, minutes: int, symbol: str) -> pd.DataFrame:
    """Same candle frame as the strategy tests feed the engine (spread column in points, last open bucket dropped)."""
    f = resample(m, minutes)
    point = POINT.get(symbol, 1.0) or 1.0
    f["spread"] = (pd.Series(m.sp).groupby(m.t // minutes).last().to_numpy() / point).round()
    if len(f) and m.t[-1] + 1 < int(f["start"].iloc[-1]) + minutes:
        f = f.iloc[:-1]
    return f[["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume", "start"]]


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    prev = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


class Context:
    """Indicators on closed candles of the zone's timeframe and of H1, looked up at a decision minute."""

    def __init__(self, m: M1, minutes: int) -> None:
        self.minutes = minutes
        tf = resample(m, minutes)
        self.tf_end = tf["start"].to_numpy(np.int64) + minutes      # candle close minute
        self.tf_atr = wilder_atr(tf["high"].to_numpy(), tf["low"].to_numpy(), tf["close"].to_numpy())
        h1 = resample(m, 60)
        self.h1_end = h1["start"].to_numpy(np.int64) + 60
        c = h1["close"].to_numpy()
        self.h1_atr = wilder_atr(h1["high"].to_numpy(), h1["low"].to_numpy(), c)
        e50 = ema(c, 50)
        self.h1_dist = (c - e50) / np.maximum(self.h1_atr, 1e-12)
        self.h1_slope = np.r_[np.zeros(5), e50[5:] - e50[:-5]] / np.maximum(self.h1_atr, 1e-12)
        self.h1_ok = np.arange(len(c)) >= 60

    def at(self, decision_min: np.ndarray, side: np.ndarray, entry: np.ndarray, sl: np.ndarray,
           top: np.ndarray, bottom: np.ndarray) -> pd.DataFrame:
        """Features known at `decision_min` (the minute the LIMIT exists): only candles closed by then."""
        i = np.searchsorted(self.tf_end, decision_min, side="right") - 1
        j = np.searchsorted(self.h1_end, decision_min, side="right") - 1
        atr = np.where(i >= 0, self.tf_atr[np.maximum(i, 0)], np.nan)
        dist = side * np.where(j >= 0, self.h1_dist[np.maximum(j, 0)], np.nan)
        slope = side * np.where(j >= 0, self.h1_slope[np.maximum(j, 0)], np.nan)
        ok = (j >= 0) & self.h1_ok[np.maximum(j, 0)]
        trend = np.where(~ok, "unknown", np.where((dist > 0) & (slope > 0), "with", np.where((dist < 0) & (slope < 0), "against", "mixed")))
        beirut = pd.to_datetime(decision_min, unit="m", utc=True).tz_convert("Asia/Beirut").hour.to_numpy()
        session = np.select([(beirut >= 3) & (beirut < 10), (beirut >= 10) & (beirut < 15), (beirut >= 15) & (beirut < 19), beirut >= 19],
                            list(SESSIONS[:4]), SESSIONS[4])
        width = np.abs(np.asarray(top, float) - np.asarray(bottom, float))
        return pd.DataFrame({"atr": atr, "sl_atr": np.abs(entry - sl) / atr, "width_atr": width / atr,
                             "h1_trend": trend, "h1_ema_dist": dist, "session": session})


# ------------------------------------------------------------------ execution
def tp_extra(symbol: str, m: M1) -> float:
    return GOLD_TP_EXTRA if symbol == "XAUUSD" else 2.0 * float(np.median(m.sp[-200_000:]))


def simulate(m: M1, side: int, entry: float, sl: float, tp: float, start: int, expire: int, commission_price: float) -> dict[str, Any] | None:
    """Christian's execution for one LIMIT on M1 index range [start, expire]; None when it never fills."""
    n = len(m)
    if start >= n or start > expire:
        return None
    je = min(expire, n - 1)
    lo, hi, sp = m.l[start:je + 1], m.h[start:je + 1], m.sp[start:je + 1]
    hit = (lo + sp <= entry) if side > 0 else (hi >= entry)
    if not hit.any():
        return None
    fj = start + int(np.argmax(hit))
    fill = min(entry, m.o[fj] + m.sp[fj]) if side > 0 else max(entry, m.o[fj])
    risk = (fill - sl) * side
    if risk <= 0:
        return None
    jend = min(fj + MAX_HOLD_MIN, n - 1)
    o, h, l, c, s = m.o[fj:jend + 1], m.h[fj:jend + 1], m.l[fj:jend + 1], m.c[fj:jend + 1], m.sp[fj:jend + 1]
    if side > 0:
        stop, take = l <= sl, h >= tp
    else:
        stop, take = h + s >= sl, l + s <= tp
    take[0] = False  # the fill minute cannot also be the TP minute (unknown order inside the candle)
    ks = int(np.argmax(stop)) if stop.any() else None
    kt = int(np.argmax(take)) if take.any() else None
    if ks is not None and (kt is None or ks <= kt):
        k, reason = ks, "SL"
        px = (sl if k == 0 else min(sl, o[k])) if side > 0 else (sl if k == 0 else max(sl, o[k] + s[k]))
    elif kt is not None:
        k, reason = kt, "TP"
        px = (tp if o[k] < tp else o[k]) if side > 0 else (tp if o[k] + s[k] > tp else o[k] + s[k])
    else:
        k, reason = len(o) - 1, "TIME"
        px = c[k] if side > 0 else c[k] + s[k]
    gross = (px - fill) * side / risk
    cost = commission_price / risk
    return {"fill_idx": fj, "exit_idx": fj + k, "fill": fill, "exit": px, "risk": risk, "reason": reason,
            "gross_r": gross, "commission_r": cost, "net_r": gross - cost,
            "spread_r": float(m.sp[fj]) / risk}


@dataclass
class Trail:
    """The bot's Trail exit (engine _manage_trail): no TP, the stop starts at the zone SL; at each candle close of the
    zone's timeframe it may move to break-even and/or trail the best price; the new stop applies from the next candle."""
    start_r: float = 1.0
    mode: str = "ATR"            # "ATR" or "Distance R" ("Candle swing" is treated as ATR)
    atr_mult: float = 1.0
    distance_r: float = 1.0
    be_r: float = 0.0
    be_lock_r: float = 0.0

    @classmethod
    def from_parameters(cls, p: dict[str, Any]) -> "Trail":
        return cls(start_r=float(p.get("trail_start_r", 1.0) or 0.0), mode=str(p.get("trail_mode", "ATR")),
                   atr_mult=float(p.get("trail_atr_mult", 2.0) or 2.0), distance_r=float(p.get("trail_distance_r", 1.0) or 1.0),
                   be_r=float(p.get("be_trigger_r", 0.0) or 0.0), be_lock_r=float(p.get("be_lock_r", 0.0) or 0.0))

    def label(self) -> str:
        how = f"{self.distance_r:g}R" if self.mode == "Distance R" else f"{self.atr_mult:g} ATR"
        be = f", stop to break-even at +{self.be_r:g}R" if self.be_r > 0 else ""
        return f"trailing stop from +{self.start_r:g}R, {how} behind the best price{be}, no TP"

    def stops(self, side: int, fill: float, sl: float, best: np.ndarray, close: np.ndarray, atr: np.ndarray | float) -> np.ndarray:
        """Stop in force DURING each candle, given each candle's best-so-far, close and ATR (all at that candle's close)."""
        risk = (fill - sl) * side
        mfe = (best - fill) * side / risk
        u = np.full(best.shape, sl * side, float)
        if self.be_r > 0:
            u = np.where(mfe >= self.be_r, np.maximum(u, (fill + side * self.be_lock_r * risk) * side), u)
        if self.start_r > 0:
            dist = self.distance_r * risk if self.mode == "Distance R" else self.atr_mult * np.asarray(atr, float)
            level = best - side * dist
            ok = (mfe >= self.start_r) & ((close - level) * side > 0)
            u = np.where(ok, np.maximum(u, level * side), u)
        after = np.maximum.accumulate(u, axis=-1)                       # the stop only moves in the trade's favour
        first = np.full(after.shape[:-1] + (1,), sl * side)
        return np.concatenate([first, after[..., :-1]], axis=-1) * side   # applies from the NEXT candle


def exit_trail(m: M1, fj: int, fill: float, side: int, sl: float, minutes: int, ctx: "Context", trail: Trail,
               commission_price: float) -> dict[str, Any]:
    """Trail exit for a trade filled at M1 index fj. Touch-triggered on M1 (buys exit on the bid, sells on the ask)."""
    n = len(m)
    jend = min(fj + MAX_HOLD_MIN, n - 1)
    t, o, h, l, c, s = (getattr(m, f)[fj:jend + 1] for f in ("t", "o", "h", "l", "c", "sp"))
    risk = (fill - sl) * side
    bucket = t // minutes
    first = np.r_[0, np.flatnonzero(np.diff(bucket)) + 1]
    last = np.r_[first[1:] - 1, len(t) - 1]
    ext = np.maximum.reduceat(h, first) if side > 0 else np.minimum.reduceat(l, first)
    best = np.maximum.accumulate(ext) if side > 0 else np.minimum.accumulate(ext)
    k_tf = np.searchsorted(ctx.tf_end, bucket[first] * minutes + minutes, side="right") - 1
    atr = np.where(k_tf >= 0, ctx.tf_atr[np.maximum(k_tf, 0)], np.nan)
    stop_b = trail.stops(side, fill, sl, best, c[last], np.nan_to_num(atr, nan=np.inf))
    stop = np.repeat(stop_b, np.diff(np.r_[first, len(t)]))
    hit = (l <= stop) if side > 0 else (h + s >= stop)
    if hit.any():
        k = int(np.argmax(hit))
        st = stop[k]
        px = (st if k == 0 else min(st, o[k])) if side > 0 else (st if k == 0 else max(st, o[k] + s[k]))
        moved = abs(st - sl) > 1e-12
        reason = "SL" if not moved else ("BE" if (px - fill) * side <= 0.25 * risk else "TRAIL")
    else:
        k = len(t) - 1
        px = c[k] if side > 0 else c[k] + s[k]
        reason = "TIME"
    gross = (px - fill) * side / risk
    cost = commission_price / risk
    return {"exit_idx": fj + k, "exit": px, "reason": reason, "gross_r": gross, "net_r": gross - cost}


def commission_price(symbol: str) -> float:
    value = VALUE_PER_PRICE_PER_LOT.get(symbol, 0.0)
    return COMMISSION_PER_LOT.get(symbol, 0.0) / value if value else 0.0


# ------------------------------------------------------------------ library
def library_hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(parameters, sort_keys=True, default=str).encode()).hexdigest()[:12]


def run_engine_zones(m: M1, symbol: str, timeframe: str, parameters: dict[str, Any]) -> pd.DataFrame:
    """Every zone the bot's engine created on these candles that got a LIMIT (entry1 + SL)."""
    from services.indicator_lab.lebprime_engine import LebPrimeEngine

    frame = engine_frame(m, TIMEFRAMES[timeframe], symbol).drop(columns=["start"])
    result = LebPrimeEngine(parameters, symbol=symbol, timeframe=timeframe).run(frame)
    rows = []
    for z in result["zones"]:
        entry = z.entry1 if z.entry1 is not None else z.entry_price
        if z.limit_created_time is None or entry is None or z.sl_price is None:
            continue
        rows.append({"zone_id": z.engine_zone_id, "side": "BUY" if str(z.side).upper().endswith("DEMAND") else "SELL",
                     "zone_top": float(z.zone_top), "zone_bottom": float(z.zone_bottom), "entry1": float(entry), "sl": float(z.sl_price),
                     "limit_time": str(z.limit_created_time), "terminal_time": str(z.terminal_time or ""),
                     "engine_state": str(z.terminal_state or ""), "base_bars": z.base_bar_count})
    return pd.DataFrame(rows, columns=["zone_id", "side", "zone_top", "zone_bottom", "entry1", "sl", "limit_time", "terminal_time",
                                       "engine_state", "base_bars"])


def trade_library(zones: pd.DataFrame, m: M1, symbol: str, timeframe: str, trail: Trail | None = None,
                  window_bars: int = LIVE_WINDOW_BARS) -> pd.DataFrame:
    """Christian's execution (TP 1R + extra, and the bot's trailing exit in tr_* columns) + decision-time features."""
    minutes = TIMEFRAMES[timeframe]
    if zones.empty:
        return zones.assign(decision_min=[], net_r=[])
    z = zones.copy()
    side = np.where(z.side == "BUY", 1, -1)
    entry, sl = z.entry1.to_numpy(float), z.sl.to_numpy(float)
    decision = to_min(z.limit_time) + minutes                 # the LIMIT exists once the candle that made it has closed
    term = z.terminal_time.replace("", np.nan)
    end_min = np.where(term.isna(), m.t[-1], to_min(term.fillna(z.limit_time)) + minutes)
    end_min = np.minimum(end_min, decision + int(window_bars) * minutes)
    extra = tp_extra(symbol, m)
    tp = entry + side * (np.abs(entry - sl) + extra)
    comm = commission_price(symbol)
    starts = np.searchsorted(m.t, decision)
    expires = np.searchsorted(m.t, end_min, side="right") - 1
    ctx = Context(m, minutes)
    trail = trail or Trail()
    out, tr = [], []
    for k in range(len(z)):
        r = simulate(m, int(side[k]), entry[k], sl[k], tp[k], int(starts[k]), int(expires[k]), comm)
        out.append(r or {})
        tr.append(exit_trail(m, int(r["fill_idx"]), r["fill"], int(side[k]), sl[k], minutes, ctx, trail, comm) if r else {})
    sim = pd.DataFrame(out, index=z.index).reindex(columns=["fill_idx", "exit_idx", "fill", "exit", "risk", "reason", "gross_r",
                                                             "commission_r", "net_r", "spread_r"])
    trs = pd.DataFrame(tr, index=z.index).reindex(columns=["exit_idx", "exit", "reason", "gross_r", "net_r"]).add_prefix("tr_")
    z = pd.concat([z, sim, trs], axis=1)
    z["decision_min"] = decision
    z["tp"] = tp
    feats = ctx.at(decision, side, entry, sl, z.zone_top.to_numpy(float), z.zone_bottom.to_numpy(float))
    feats.index = z.index
    z = pd.concat([z, feats], axis=1)
    filled = z["fill_idx"].notna() if "fill_idx" in z else pd.Series(False, index=z.index)
    z["filled"] = filled
    z["exit_min"] = np.where(filled, m.t[z["exit_idx"].fillna(0).astype(np.int64).to_numpy()], -1)
    z["exit_time"] = pd.to_datetime(np.where(filled, z["exit_min"], 0), unit="m", utc=True).strftime("%Y-%m-%dT%H:%M")
    z.loc[~filled, "exit_time"] = ""
    z["tr_exit_min"] = np.where(filled, m.t[z["tr_exit_idx"].fillna(0).astype(np.int64).to_numpy()], -1)
    z["tr_exit_time"] = pd.to_datetime(np.where(filled, z["tr_exit_min"], 0), unit="m", utc=True).strftime("%Y-%m-%dT%H:%M")
    z.loc[~filled, "tr_exit_time"] = ""
    return z


def exit_view(lib: pd.DataFrame, exit_name: str) -> pd.DataFrame:
    """The library seen through one exit: "tp" (1R + extra) or "trail" (the bot's trailing exit)."""
    if exit_name != "trail" or "tr_net_r" not in lib:
        return lib
    view = lib.copy()
    for col in ("exit_idx", "exit", "reason", "gross_r", "net_r", "exit_min", "exit_time"):
        view[col] = lib[f"tr_{col}"]
    return view


# ------------------------------------------------------------------ analogs
def wilson(wins: int, n: int, zscore: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + zscore ** 2 / n
    centre = (p + zscore ** 2 / (2 * n)) / denom
    half = zscore * math.sqrt(p * (1 - p) / n + zscore ** 2 / (4 * n * n)) / denom
    return (round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1))


def stats(g: pd.DataFrame) -> dict[str, Any]:
    n = len(g)
    if not n:
        return {"trades": 0}
    r = g["net_r"].to_numpy(float)
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.r_[0.0, np.cumsum(r[np.argsort(g["exit_min"].to_numpy())])]
    avg_w = float(wins.mean()) if len(wins) else None
    avg_l = float(losses.mean()) if len(losses) else None
    out = {"trades": n, "win_pct": round(100 * len(wins) / n, 1), "win_ci95": wilson(len(wins), n),
           "net_r": round(float(r.sum()), 2), "avg_r": round(float(r.mean()), 3),
           "avg_win_r": round(avg_w, 3) if avg_w is not None else None, "avg_loss_r": round(avg_l, 3) if avg_l is not None else None,
           "breakeven_win_pct": round(100 * -avg_l / (avg_w - avg_l), 1) if avg_w is not None and avg_l is not None and avg_w - avg_l > 0 else None,
           "cost_r": round(float((g["commission_r"] + g["spread_r"]).sum()), 2), "max_dd_r": round(float((np.maximum.accumulate(eq) - eq).max()), 2),
           "tp": int(g["reason"].isin(["TP", "TRAIL"]).sum()), "be": int((g["reason"] == "BE").sum()), "sl": int((g["reason"] == "SL").sum()),
           "time_exit": int((g["reason"] == "TIME").sum()),
           "trusted": n >= MIN_TRUST}
    return out


def by_year(g: pd.DataFrame) -> dict[str, Any]:
    if not len(g):
        return {}
    year = g["exit_time"].str[:4]
    return {y: stats(part) for y, part in g.groupby(year)}


def side_split(g: pd.DataFrame) -> dict[str, Any]:
    return {"buys": stats(g[g.side == "BUY"]), "sells": stats(g[g.side == "SELL"])}


def width_bucket(values: pd.Series, edges: tuple[float, float]) -> pd.Series:
    v = pd.to_numeric(values, errors="coerce").to_numpy(float)
    out = np.where(v <= edges[0], "narrow", np.where(v > edges[1], "wide", "medium"))
    return pd.Series(np.where(np.isnan(v), "unknown", out), index=values.index)


def analogs(lib: pd.DataFrame, target: dict[str, Any]) -> dict[str, Any]:
    """Past trades that look like the target, from strict to loose. Only trades that had exited by the target's decision time."""
    past = lib[lib["filled"] & (lib["exit_min"] < target["decision_min"]) & lib["sl_atr"].notna()].copy()
    if past.empty:
        return {"tiers": [], "headline": None, "past_trades": 0}
    edges = tuple(np.nanquantile(past["width_atr"], [1 / 3, 2 / 3]).round(3)) if len(past) >= 3 else (0.5, 1.0)
    past["width_bucket"] = width_bucket(past["width_atr"], edges)
    t_width = str(width_bucket(pd.Series([target.get("width_atr") or np.nan]), edges).iloc[0])
    same_side = past[past.side == target["side"]]
    sl_ok = same_side["sl_atr"].between(target["sl_atr"] * SL_BAND[0], target["sl_atr"] * SL_BAND[1])
    trend_ok = same_side["h1_trend"] == target["h1_trend"]
    session_ok = same_side["session"] == target["session"]
    known_width = target.get("width_atr") is not None and np.isfinite(target["width_atr"])
    width_ok = same_side["width_bucket"] == t_width if known_width else pd.Series(True, index=same_side.index)
    sl_text = f"SL {target['sl_atr'] * SL_BAND[0]:.2f}-{target['sl_atr'] * SL_BAND[1]:.2f} ATR"
    specs = [
        ("closest", "Same side, similar SL, same H1 trend, same session, same zone width", sl_ok & trend_ok & session_ok & width_ok),
        ("no_width", "Same side, similar SL, same H1 trend, same session", sl_ok & trend_ok & session_ok),
        ("no_session", "Same side, similar SL, same H1 trend, same zone width", sl_ok & trend_ok & width_ok),
        ("sl_trend", "Same side, similar SL, same H1 trend", sl_ok & trend_ok),
        ("sl_only", "Same side, similar SL", sl_ok),
        ("side", f"Every {target['side'].lower()} zone on this symbol and timeframe", pd.Series(True, index=same_side.index)),
    ]
    tiers = []
    for key, label, mask in specs:
        g = same_side[mask]
        tiers.append({"key": key, "label": label, **stats(g), "by_year": by_year(g)})
    headline = next((t for t in tiers if t["trades"] >= MIN_TRUST), tiers[-1])
    sample = same_side[sl_ok & trend_ok & session_ok & width_ok].sort_values("decision_min").tail(25)
    return {
        "past_trades": int(len(past)), "conditions": {"side": target["side"], "sl": sl_text, "h1_trend": target["h1_trend"],
                                                      "session": target["session"], "zone_width": (f"{t_width} ({target['width_atr']:.2f} ATR; terciles {edges[0]:.2f} / {edges[1]:.2f})" if known_width else "not given (ignored)")},
        "tiers": tiers, "headline": headline["key"],
        "all_zones": {**stats(past), **side_split(past), "by_year": by_year(past)},
        "closest_examples": [
            {"limit_time": r.limit_time, "exit_time": r.exit_time, "entry1": round(r.entry1, 5), "sl": round(r.sl, 5), "sl_atr": round(r.sl_atr, 2),
             "reason": r.reason, "net_r": round(r.net_r, 3)} for r in sample.itertuples()],
    }


# ------------------------------------------------------------------ Monte Carlo
def monte_carlo(m: M1, side: int, entry: float, sl: float, tp: float, commission: float, paths: int = 10_000,
                block: int = 60, horizon: int = MAX_HOLD_MIN, seed: int = 7, demean: bool = True) -> dict[str, Any]:
    """Block bootstrap of the given (recent, pre-decision) M1 candles into `paths` paths that start filled at Entry 1.

    Each simulated minute reuses a real minute's candle shape (open->close, high, low, spread). `demean` removes the
    recent average drift so the answer does not just extrapolate the last weeks' trend.
    """
    n = len(m)
    if n < block * 5:
        raise ValueError("not enough recent candles for the Monte Carlo")
    rng = np.random.default_rng(seed)
    prev_close = np.r_[m.o[0], m.c[:-1]]
    gap = m.o - prev_close
    dc = m.c - m.o + gap  # close-to-close move, so weekend/rollover gaps are part of the paths
    dh, dl = m.h - m.o + gap, m.l - m.o + gap
    if demean:
        mu = dc.mean()
        dc, dh, dl = dc - mu, dh - mu, dl - mu
    risk = abs(entry - sl)
    bid0 = entry - m.sp[-1] if side > 0 else entry   # a buy LIMIT fills when the ask touches Entry 1
    nblocks = -(-horizon // block)
    tp_hits = sl_hits = 0
    time_r: list[np.ndarray] = []
    first_hit_min: list[np.ndarray] = []
    chunk = 500
    for done in range(0, paths, chunk):
        k = min(chunk, paths - done)
        starts = rng.integers(0, n - block, size=(k, nblocks))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(k, -1)[:, :horizon]
        c_rel = np.cumsum(dc[idx], axis=1)
        o_rel = np.concatenate([np.zeros((k, 1)), c_rel[:, :-1]], axis=1)
        # o_rel = previous simulated close; dh/dl are each real minute's high/low measured from ITS previous close
        hi = bid0 + o_rel + dh[idx]
        lo = bid0 + o_rel + dl[idx]
        spr = m.sp[idx]
        if side > 0:
            stop, take = lo <= sl, hi >= tp
        else:
            stop, take = hi + spr >= sl, lo + spr <= tp
        big = horizon + 1
        ks = np.where(stop.any(axis=1), stop.argmax(axis=1), big)
        kt = np.where(take.any(axis=1), take.argmax(axis=1), big)
        is_sl = (ks <= kt) & (ks < big)
        is_tp = (kt < ks)
        sl_hits += int(is_sl.sum())
        tp_hits += int(is_tp.sum())
        neither = ~(is_sl | is_tp)
        if neither.any():
            last_bid = bid0 + c_rel[neither, -1]
            exit_px = last_bid if side > 0 else last_bid + spr[neither, -1]
            time_r.append((exit_px - entry) * side / risk)
        first_hit_min.append(np.minimum(ks, kt)[~neither])
    timeouts = paths - tp_hits - sl_hits
    tp_r = abs(tp - entry) / risk
    comm_r = commission / risk
    time_avg = float(np.concatenate(time_r).mean()) if time_r else 0.0
    exp_r = (tp_hits * tp_r - sl_hits * 1.0 + timeouts * time_avg) / paths - comm_r
    hits = np.concatenate(first_hit_min) if first_hit_min else np.array([])
    p_tp = tp_hits / paths
    return {"paths": paths, "p_tp_first": round(100 * p_tp, 1), "p_sl_first": round(100 * sl_hits / paths, 1),
            "p_neither_3d": round(100 * timeouts / paths, 1), "ci95": wilson(tp_hits, paths),
            "expected_r": round(exp_r, 3), "tp_r": round(tp_r - comm_r, 3), "sl_r": round(-1 - comm_r, 3),
            "median_minutes_to_outcome": int(np.median(hits)) if len(hits) else None,
            "breakeven_win_pct": round(100 * (1 + comm_r) / (tp_r + 1), 1), "demeaned": demean, "block_minutes": block}


def monte_carlo_trail(m: M1, side: int, entry: float, sl: float, commission: float, trail: Trail, minutes: int, atr: float,
                      paths: int = 10_000, block: int = 60, horizon: int = MAX_HOLD_MIN, seed: int = 7, demean: bool = True) -> dict[str, Any]:
    """Same block-bootstrap paths as monte_carlo(), traded with the bot's trailing exit (stop moves at each candle close of
    the zone's timeframe, counted from the fill; the ATR is the zone's ATR when the LIMIT was placed)."""
    n = len(m)
    if n < block * 5:
        raise ValueError("not enough recent candles for the Monte Carlo")
    rng = np.random.default_rng(seed)
    prev_close = np.r_[m.o[0], m.c[:-1]]
    gap = m.o - prev_close
    dc, dh, dl = m.c - m.o + gap, m.h - m.o + gap, m.l - m.o + gap
    if demean:
        mu = dc.mean()
        dc, dh, dl = dc - mu, dh - mu, dl - mu
    risk = abs(entry - sl)
    bid0 = entry - m.sp[-1] if side > 0 else entry
    horizon = -(-horizon // minutes) * minutes
    nb = horizon // minutes
    nblocks = -(-horizon // block)
    results: list[np.ndarray] = []
    reasons = {"SL": 0, "BE": 0, "TRAIL": 0, "TIME": 0}
    for done in range(0, paths, 500):
        k = min(500, paths - done)
        starts = rng.integers(0, n - block, size=(k, nblocks))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(k, -1)[:, :horizon]
        c_rel = np.cumsum(dc[idx], axis=1)
        o_rel = np.concatenate([np.zeros((k, 1)), c_rel[:, :-1]], axis=1)
        op, hi, lo, spr = bid0 + o_rel, bid0 + o_rel + dh[idx], bid0 + o_rel + dl[idx], m.sp[idx]
        ext = hi.reshape(k, nb, minutes).max(axis=2) if side > 0 else lo.reshape(k, nb, minutes).min(axis=2)
        best = np.maximum.accumulate(ext, axis=1) if side > 0 else np.minimum.accumulate(ext, axis=1)
        close = bid0 + c_rel[:, minutes - 1::minutes]
        stop = np.repeat(trail.stops(side, entry, sl, best, close, atr), minutes, axis=1)
        hit = (lo <= stop) if side > 0 else (hi + spr >= stop)
        any_hit = hit.any(axis=1)
        kk = hit.argmax(axis=1)
        rows = np.arange(k)
        st, o_k, s_k = stop[rows, kk], op[rows, kk], spr[rows, kk]
        px = np.where(kk == 0, st, np.minimum(st, o_k)) if side > 0 else np.where(kk == 0, st, np.maximum(st, o_k + s_k))
        last_px = (bid0 + c_rel[:, -1]) if side > 0 else (bid0 + c_rel[:, -1] + spr[:, -1])
        px = np.where(any_hit, px, last_px)
        r = (px - entry) * side / risk - commission / risk
        moved = np.abs(st - sl) > 1e-12
        reasons["TIME"] += int((~any_hit).sum())
        reasons["SL"] += int((any_hit & ~moved).sum())
        be = any_hit & moved & ((px - entry) * side <= 0.25 * risk)
        reasons["BE"] += int(be.sum())
        reasons["TRAIL"] += int((any_hit & moved & ~be).sum())
        results.append(r)
    r = np.concatenate(results)
    wins = int((r > 0).sum())
    w, lss = r[r > 0], r[r <= 0]
    return {"paths": paths, "win_pct": round(100 * wins / paths, 1), "ci95": wilson(wins, paths), "expected_r": round(float(r.mean()), 3),
            "avg_win_r": round(float(w.mean()), 3) if len(w) else None, "avg_loss_r": round(float(lss.mean()), 3) if len(lss) else None,
            "p_sl": round(100 * reasons["SL"] / paths, 1), "p_be": round(100 * reasons["BE"] / paths, 1),
            "p_trail": round(100 * reasons["TRAIL"] / paths, 1), "p_neither_3d": round(100 * reasons["TIME"] / paths, 1),
            "demeaned": demean, "block_minutes": block}


def p_fill(m: M1, side: int, entry: float, spread_now: float, last_close: float, minutes_left: int, paths: int = 4000, block: int = 60, seed: int = 11) -> float | None:
    """Monte Carlo chance the LIMIT is touched within its remaining life, from the latest close (drift removed)."""
    if minutes_left <= 0 or len(m) < block * 5:
        return None
    if (side > 0 and last_close + spread_now <= entry) or (side < 0 and last_close >= entry):
        return 100.0
    rng = np.random.default_rng(seed)
    prev_close = np.r_[m.o[0], m.c[:-1]]
    gap = m.o - prev_close
    dc = m.c - m.o + gap
    mu = dc.mean()
    dc, dh, dl = dc - mu, m.h - m.o + gap - mu, m.l - m.o + gap - mu
    horizon = int(min(minutes_left, 20_000))
    nblocks = -(-horizon // block)
    hit = 0
    for done in range(0, paths, 500):
        k = min(500, paths - done)
        starts = rng.integers(0, len(m) - block, size=(k, nblocks))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(k, -1)[:, :horizon]
        o_rel = np.concatenate([np.zeros((k, 1)), np.cumsum(dc[idx], axis=1)[:, :-1]], axis=1)
        if side > 0:
            hit += int(((last_close + o_rel + dl[idx] + m.sp[idx]) <= entry).any(axis=1).sum())
        else:
            hit += int(((last_close + o_rel + dh[idx]) >= entry).any(axis=1).sum())
    return round(100 * hit / paths, 1)


Note = Callable[[str], None]
