"""Strategy test started from the dashboard's Indicator Lab tab.

    python scripts/strategy_test.py <job-folder>

The dashboard writes <job-folder>/request.json, the root dispatcher (deploy/linux/bin/
lebprime-research-dispatch) starts this script inside the capped research sandbox
(lebprime-research: no secrets, read-only live config and controls, lowest priority).
It never talks to MT5 or the live bot.

What it does, per symbol:
* loads closed M1 candles from the MT5 research store (/var/lib/lebprime/mt5_data/M1/<SYM>.csv),
  falling back to the newest frozen indicator dataset, and builds the timeframe from closed M1 bars;
* runs the bot's own LebPrimeEngine (services/indicator_lab) with the requested parameters. The engine
  walks candles one at a time and only sees closed candles, so there is no look-ahead. The
  TradingView-only "recent history window" (needs the last bar of the file) is forced off;
* charges real broker costs per trade: the candle spread at entry plus the round-trip commission per
  lot (phase4_search cost model, measured on the demo account);
* reports year by year (by exit time) per symbol: trades, win %, net R, avg win / avg loss R,
  buys vs sells, plus max drawdown in R.

Outputs in the job folder: status.json (progress), trades.csv (one row per trade, readable by the
Research page) and summary.json (the tables the dashboard shows).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MT5_STORE = Path(os.environ.get("LEBPRIME_MT5_STORE", "/var/lib/lebprime/mt5_data/M1"))
DATASET_DIR = ROOT / "storage" / "indicator_datasets"
FINAL_STATES = {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}
WARMUP_BARS = 2000
SYMBOLS = ("XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD")
TIMEFRAMES = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M10": 10, "M15": 15, "M30": 30, "H1": 60, "H4": 240}
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.request = json.loads((folder / "request.json").read_text())
        self.status: dict = {"state": "RUNNING", "started_at": now(), "progress": [], "pid": os.getpid()}
        self.save()

    def save(self) -> None:
        self.status["updated_at"] = now()
        tmp = self.folder / "status.json.tmp"
        tmp.write_text(json.dumps(self.status, indent=1))
        os.replace(tmp, self.folder / "status.json")

    def note(self, message: str) -> None:
        print(message, flush=True)
        self.status["progress"].append(f"{now()[11:19]} {message}")
        self.status["progress"] = self.status["progress"][-40:]
        self.save()


# ------------------------------------------------------------------ data
def m1_source(symbol: str) -> Path:
    store = MT5_STORE / f"{symbol}.csv"
    if store.is_file():
        return store
    frozen = sorted(DATASET_DIR.glob(f"dataset_*_{symbol}_M1.csv"))
    if not frozen:
        raise FileNotFoundError(f"no M1 data for {symbol} in {MT5_STORE} or {DATASET_DIR}")
    return frozen[-1]


def load_frame(symbol: str, timeframe: str, start: str, end: str) -> tuple[pd.DataFrame, str]:
    """Closed candles of ``timeframe`` from START (minus warm-up) to END (exclusive), built from M1."""
    minutes = TIMEFRAMES[timeframe]
    source = m1_source(symbol)
    warm_start = (pd.Timestamp(start, tz="UTC") - pd.Timedelta(minutes=minutes * WARMUP_BARS * 1.6)).isoformat()
    chunks = []
    for chunk in pd.read_csv(source, chunksize=500_000, dtype={"timestamp_utc": str}):
        ts = chunk["timestamp_utc"]
        chunk = chunk[(ts >= warm_start[:19]) & (ts < end)]
        if len(chunk):
            chunks.append(chunk)
    if not chunks:
        raise ValueError(f"{symbol}: no candles between {start} and {end} in {source.name}")
    d = pd.concat(chunks, ignore_index=True)
    if minutes > 1:
        t = pd.to_datetime(d["timestamp_utc"], utc=True)
        d["g"] = (t - EPOCH) // pd.Timedelta(minutes=minutes)
        d = d.groupby("g").agg(timestamp_utc=("timestamp_utc", "first"), open=("open", "first"), high=("high", "max"),
                               low=("low", "min"), close=("close", "last"), tick_volume=("tick_volume", "sum"),
                               spread=("spread", "last"), real_volume=("real_volume", "sum"))
        d["timestamp_utc"] = pd.to_datetime(d.index * minutes, unit="m", utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        # The last bucket may still be forming when the store was updated mid-candle: drop it.
        last_m1 = pd.Timestamp(chunks[-1]["timestamp_utc"].iloc[-1])
        if last_m1 + pd.Timedelta(minutes=1) < pd.Timestamp(d["timestamp_utc"].iloc[-1]) + pd.Timedelta(minutes=minutes):
            d = d.iloc[:-1]
    idx = np.flatnonzero(d["timestamp_utc"].to_numpy().astype(str) >= start[:10])
    begin = max(0, int(idx[0]) - WARMUP_BARS) if len(idx) else 0
    return d.iloc[begin:].reset_index(drop=True), source.name


# ------------------------------------------------------------------ test
def run_symbol(symbol: str, timeframe: str, start: str, end: str, parameters: dict) -> tuple[list[dict], str, int]:
    from phase4_search import COMMISSION_PER_LOT, POINT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine

    frame, source = load_frame(symbol, timeframe, start, end)
    result = LebPrimeEngine(parameters, symbol=symbol, timeframe=timeframe).run(frame)
    point, value, commission = POINT.get(symbol, 0.0), VALUE_PER_PRICE_PER_LOT.get(symbol, 0.0), COMMISSION_PER_LOT.get(symbol, 0.0)
    single = parameters.get("entry2_mode") == "Off" or parameters.get("entry_mode") == "Close confirmation"
    rows = []
    for z in result["zones"]:
        if z.terminal_state not in FINAL_STATES or z.entry1_activated_time is None:
            continue
        if str(z.limit_created_time) < start:
            continue
        risk = abs(float(z.entry_price) - float(z.sl_price))
        if not risk:
            continue
        filled = sum(1 for leg in (z.metadata.get("live_legs") or {}).values() if leg.get("filled")) or 1
        weight = 1.0 if filled == 1 and single else 0.5
        spread_r = float(z.metadata.get("entry1_spread") or 0.0) * point / risk * weight * filled
        commission_r = (commission / (risk * value)) * weight * filled if value else 0.0
        gross = float(z.result_r or 0.0)
        rows.append({
            "symbol": symbol, "tf": timeframe, "zone_id": z.engine_zone_id, "side": "BUY" if z.side == "DEMAND" else "SELL",
            "limit_time": z.limit_created_time, "entry_time": z.entry1_activated_time, "exit_time": z.terminal_time,
            "exit_state": z.terminal_state, "entries_filled": filled, "risk_price": round(risk, 6),
            "gross_r": round(gross, 4), "cost_r": round(spread_r + commission_r, 4), "net_r": round(gross - spread_r - commission_r, 4),
        })
    return rows, source, len(frame)


def stats(g: pd.DataFrame) -> dict:
    n = len(g)
    if not n:
        return {"trades": 0}
    wins, losses = g[g.net_r > 0].net_r, g[g.net_r <= 0].net_r
    eq = np.r_[0.0, g.sort_values("exit_time").net_r.cumsum().to_numpy()]
    out = {"trades": n, "win_pct": round(len(wins) / n * 100, 1), "net_r": round(g.net_r.sum(), 2),
           "gross_r": round(g.gross_r.sum(), 2), "cost_r": round(g.cost_r.sum(), 2),
           "avg_win_r": round(wins.mean(), 3) if len(wins) else None, "avg_loss_r": round(losses.mean(), 3) if len(losses) else None,
           "max_dd_r": round(float((np.maximum.accumulate(eq) - eq).max()), 2)}
    for side, label in (("BUY", "buys"), ("SELL", "sells")):
        s = g[g.side == side]
        out[label] = {"trades": len(s), "win_pct": round((s.net_r > 0).mean() * 100, 1) if len(s) else None, "net_r": round(s.net_r.sum(), 2)}
    return out


def summarise(trades: pd.DataFrame, symbols: list[str]) -> dict:
    out = {"symbols": {}, "years": sorted(trades.exit_time.astype(str).str[:4].unique().tolist()) if len(trades) else []}
    groups = [(s, trades[trades.symbol == s]) for s in symbols] + [("ALL", trades)]
    for name, g in groups:
        if not len(g):
            out["symbols"][name] = {"total": {"trades": 0}, "by_year": {}}
            continue
        year = g.exit_time.astype(str).str[:4]
        by_year = {y: stats(part) for y, part in g.groupby(year)}
        out["symbols"][name] = {"total": stats(g), "by_year": by_year,
                                "losing_years": sum(1 for v in by_year.values() if v["net_r"] <= 0)}
    return out


def main(folder: Path) -> None:
    job = Job(folder)
    try:
        from services.indicator_lab.parameter_registry import validate_parameters

        req = job.request
        symbols = [s for s in req["symbols"] if s in SYMBOLS]
        timeframe, start, end = req["timeframe"], req["start"], req["end"]
        if timeframe not in TIMEFRAMES or not symbols:
            raise ValueError("bad symbols or timeframe")
        parameters = validate_parameters("LEBPRIME", req.get("parameters") or {})
        parameters["apply_history_window"] = False  # needs the file's last bar: look-ahead, TradingView comparison only
        per_symbol = req.get("symbol_parameters") or {}
        all_rows, sources = [], {}
        for symbol in symbols:
            t0 = time.time()
            job.note(f"{symbol} {timeframe}: loading candles and running the engine")
            params = validate_parameters("LEBPRIME", {**parameters, **(per_symbol.get(symbol) or {})})
            params["apply_history_window"] = False
            rows, source, bars = run_symbol(symbol, timeframe, start, end, params)
            sources[symbol] = {"file": source, "bars": bars}
            all_rows += rows
            job.note(f"{symbol} {timeframe}: {len(rows)} trades, {sum(r['net_r'] for r in rows):+.1f}R net ({time.time() - t0:.0f} s)")
        trades = pd.DataFrame(all_rows, columns=["symbol", "tf", "zone_id", "side", "limit_time", "entry_time", "exit_time", "exit_state",
                                                 "entries_filled", "risk_price", "gross_r", "cost_r", "net_r"])
        trades.sort_values("exit_time").to_csv(folder / "trades.csv", index=False)
        summary = {"request": req, "data": sources, "parameters": parameters, "finished_at": now(),
                   "method": "LebPrimeEngine on closed candles (no look-ahead); net R after candle spread at entry + round-trip commission; years by exit time",
                   **summarise(trades, symbols)}
        (folder / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
        job.status["state"] = "COMPLETED"
        job.note("done")
    except Exception as exc:  # report the failure to the dashboard instead of dying silently
        job.status["state"] = "FAILED"
        job.status["error"] = f"{type(exc).__name__}: {exc}"
        job.status["traceback"] = traceback.format_exc()[-4000:]
        job.note("failed")
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
