"""Time Machine job: honest odds for one zone, started from the dashboard's Indicator Lab tab.

The dashboard writes <job-folder>/request.json with "kind": "time_machine"; the research-sandbox dispatcher
starts scripts/strategy_test.py for it, which hands it to run_job() here. It never talks to MT5 or the live bot.

Steps (see services/indicator_lab/time_machine.py for the rules):
1. load closed M1 candles for the symbol from the MT5 research store;
2. load or build the zone library: the bot's own engine over past candles with the live bot's parameters,
   every zone traded with Christian's execution, real spread + commission (cached per symbol/timeframe,
   rebuilt when older than 7 days or when the parameters change);
3. measure the zone at the minute its LIMIT exists (closed candles only) and pick past trades that had
   already exited by then and look like it;
4. Monte Carlo of 3-day paths from the 20 trading days of M1 candles before that minute;
5. if candles after that minute exist (an old zone), what really happened, kept apart from the odds.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from services.indicator_lab import time_machine as tm

MT5_STORE = Path(os.environ.get("LEBPRIME_MT5_STORE", "/var/lib/lebprime/mt5_data/M1"))
LIB_MAX_AGE = timedelta(days=7)
LIB_YEARS = {1: 2, 2: 3, 3: 3}  # library history for the fast timeframes (engine time); others use all data since 2020
LIB_FIRST = "2020-01-01"
MC_DAYS_DEFAULT = 20


def live_parameters(root: Path) -> dict[str, Any]:
    """LEBPRIME parameters as the live bot runs them (defaults + config overrides), read-only."""
    from services.indicator_lab.parameter_registry import default_parameters, validate_parameters

    overrides: dict[str, Any] = {}
    try:
        config = json.loads((root / "config.json").read_text())
        overrides = dict((config.get("lebprime_v3_4_0") or {}).get("parameters") or {})
    except (OSError, ValueError):
        pass
    try:
        params = validate_parameters("LEBPRIME", {**default_parameters("LEBPRIME"), **overrides})
    except (ValueError, TypeError):
        params = default_parameters("LEBPRIME")
    params["apply_history_window"] = False          # needs the file's last bar: look-ahead
    if not int(params.get("limit_expiry_bars") or 0):
        params["limit_expiry_bars"] = tm.LIVE_WINDOW_BARS   # the live bot only sees its last 360 candles
    return params


def m1_path(symbol: str, root: Path) -> Path:
    store = MT5_STORE / f"{symbol}.csv"
    if store.is_file():
        return store
    frozen = sorted((root / "storage" / "indicator_datasets").glob(f"dataset_*_{symbol}_M1.csv"))
    if not frozen:
        raise FileNotFoundError(f"no M1 data for {symbol}")
    return frozen[-1]


def library(job: Any, lib_dir: Path, m: tm.M1, symbol: str, timeframe: str, params: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    lib_dir.mkdir(parents=True, exist_ok=True)
    key = f"{symbol}_{timeframe}"
    meta_path, data_path = lib_dir / f"{key}.json", lib_dir / f"{key}.pkl"
    digest = tm.library_hash(params)
    try:
        meta = json.loads(meta_path.read_text())
        fresh = datetime.now(timezone.utc) - datetime.fromisoformat(meta["built_at"]) < LIB_MAX_AGE
        if meta.get("params_hash") == digest and fresh and data_path.is_file():
            job.note(f"zone library: reusing {meta['zones']} zones / {meta['trades']} trades built {meta['built_at'][:16]} (data until {meta['data_end'][:16]})")
            return pd.read_pickle(data_path), meta
    except (OSError, ValueError, KeyError):
        pass
    minutes = tm.TIMEFRAMES[timeframe]
    years = LIB_YEARS.get(minutes)
    start_min = int(m.t[-1] - years * 365 * 1440) if years else int(tm.to_min(LIB_FIRST)[0])
    part = m.slice(start_min, None)
    job.note(f"zone library: running the bot's engine on {symbol} {timeframe} from {pd.to_datetime(part.t[0], unit='m'):%Y-%m-%d} "
             f"({len(part) // minutes:,} candles). First run per symbol/timeframe only; this can take a while")
    t0 = time.time()
    zones = tm.run_engine_zones(part, symbol, timeframe, params)
    job.note(f"zone library: {len(zones)} zones in {time.time() - t0:.0f} s; trading them with your execution")
    lib = tm.trade_library(zones, m, symbol, timeframe)
    meta = {"symbol": symbol, "timeframe": timeframe, "params_hash": digest, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_start": pd.to_datetime(part.t[0], unit="m", utc=True).isoformat(), "data_end": pd.to_datetime(m.t[-1], unit="m", utc=True).isoformat(),
            "zones": int(len(lib)), "trades": int(lib["filled"].sum()) if len(lib) else 0, "engine_seconds": round(time.time() - t0)}
    tmp = data_path.with_suffix(".tmp")
    lib.to_pickle(tmp)
    os.replace(tmp, data_path)
    meta_path.write_text(json.dumps(meta, indent=1))
    return lib, meta


def run_job(job: Any, root: Path) -> dict[str, Any]:
    req = job.request
    symbol = tm.canonical_symbol(req["symbol"])
    timeframe = req["timeframe"]
    side_word = req["side"].upper()
    side = 1 if side_word == "BUY" else -1
    entry, sl = float(req["entry1"]), float(req["sl"])
    minutes = tm.TIMEFRAMES[timeframe]
    if symbol not in tm.SYMBOLS or (entry - sl) * side <= 0:
        raise ValueError("bad zone: unknown symbol or SL on the wrong side of Entry 1")

    job.note(f"{symbol} {timeframe} {side_word} E1 {entry} SL {sl}: loading M1 candles")
    m = tm.load_m1(m1_path(symbol, root), symbol, start=LIB_FIRST)
    data_end = int(m.t[-1]) + 1
    if req.get("limit_time"):
        decision = int(tm.to_min(req["limit_time"])[0]) + minutes
    else:
        decision = data_end
    decision = min(decision, data_end)
    decision_iso = pd.to_datetime(decision, unit="m", utc=True).isoformat()

    params = live_parameters(root)
    lib, lib_meta = library(job, job.folder.parent.parent / "time_machine_library", m, symbol, timeframe, params)

    extra = tm.tp_extra(symbol, m.slice(None, decision))
    tp = entry + side * (abs(entry - sl) + extra)
    top = req.get("zone_top")
    bottom = req.get("zone_bottom")
    feats = tm.Context(m.slice(None, decision), minutes).at(np.array([decision]), np.array([side]), np.array([entry]), np.array([sl]),
                                                           np.array([top if top is not None else np.nan], float),
                                                           np.array([bottom if bottom is not None else np.nan], float)).iloc[0]
    target = {"side": side_word, "decision_min": decision, "sl_atr": float(feats.sl_atr), "width_atr": float(feats.width_atr),
              "h1_trend": str(feats.h1_trend), "session": str(feats.session), "atr": float(feats.atr)}
    job.note(f"zone at {decision_iso[:16]} UTC: SL {target['sl_atr']:.2f} ATR, H1 trend {target['h1_trend']}, session {target['session']}")

    found = tm.analogs(lib, target)
    head = next((t for t in found.get("tiers", []) if t["key"] == found.get("headline")), None)
    if head:
        job.note(f"analogs: {head['trades']} similar past trades, win {head.get('win_pct')}%, avg {head.get('avg_r')}R ({head['label']})")

    paths = int(req.get("paths") or 10_000)
    mc_days = int(req.get("mc_days") or MC_DAYS_DEFAULT)
    recent = m.slice(None, decision)
    recent = recent.slice(int(recent.t[-1] - mc_days * 1.45 * 1440), None)  # ~mc_days trading days of minutes before the decision
    commission = tm.commission_price(symbol)
    job.note(f"Monte Carlo: {paths:,} paths from {len(recent):,} recent M1 candles")
    mc = tm.monte_carlo(recent, side, entry, sl, tp, commission, paths=paths, demean=True)
    mc_drift = tm.monte_carlo(recent, side, entry, sl, tp, commission, paths=paths, demean=False, seed=8)

    life_end = (int(tm.to_min(req["limit_time"])[0]) if req.get("limit_time") else decision - minutes) + minutes * (tm.LIVE_WINDOW_BARS + 1)
    actual = None
    start = int(np.searchsorted(m.t, decision))
    if start < len(m) - 1:
        expire = int(np.searchsorted(m.t, life_end, side="right")) - 1
        r = tm.simulate(m, side, entry, sl, tp, start, expire, commission)
        horizon_done = m.t[-1] >= life_end if r is None else (r["reason"] != "TIME" or m.t[-1] >= m.t[r["fill_idx"]] + tm.MAX_HOLD_MIN)
        actual = {"filled": r is not None, "final": bool(horizon_done)}
        if r:
            actual.update({"fill_time": pd.to_datetime(m.t[r["fill_idx"]], unit="m", utc=True).isoformat(),
                           "exit_time": pd.to_datetime(m.t[r["exit_idx"]], unit="m", utc=True).isoformat(),
                           "reason": r["reason"], "net_r": round(r["net_r"], 3)})

    fill = None
    if life_end > data_end and (actual is None or not actual["filled"]):
        # the LIMIT is still waiting now: chance it is touched before it expires, from the latest candles
        latest = m.slice(int(m.t[-1] - mc_days * 1.45 * 1440), None)
        fill = tm.p_fill(latest, side, entry, float(latest.sp[-1]), float(latest.c[-1]), life_end - data_end)

    return {
        "zone": {"symbol": symbol, "timeframe": timeframe, "side": side_word, "entry1": entry, "sl": sl, "tp": round(tp, 6),
                 "tp_extra": round(extra, 6), "risk": abs(entry - sl), "zone_top": top, "zone_bottom": bottom,
                 "decision_time": decision_iso, "source": req.get("source") or "manual", "live_zone_id": req.get("live_zone_id")},
        "features": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in target.items() if k != "decision_min"},
        "analogs": found, "monte_carlo": mc, "monte_carlo_with_drift": mc_drift, "p_fill": fill, "actual": actual,
        "library": lib_meta, "parameters": params,
        "data_until": pd.to_datetime(m.t[-1], unit="m", utc=True).isoformat(), "mc_window": {
            "from": pd.to_datetime(recent.t[0], unit="m", utc=True).isoformat(), "to": pd.to_datetime(recent.t[-1], unit="m", utc=True).isoformat(),
            "candles": int(len(recent))},
        "method": ("Analogs: the bot's engine (live parameters, LIMIT cancelled after 360 candles) on past candles; one LIMIT at Entry 1, zone SL, "
                   "TP = 1R + extra (gold $0.20, others 2x median spread), touch-triggered on M1 with the real spread (buys fill on the ask), "
                   "SL wins a same-minute tie, closed at market after 3 days, net of round-trip commission. Only trades that had exited before "
                   "this zone's LIMIT existed are counted, and every indicator uses closed candles only. Monte Carlo: block bootstrap (60-minute "
                   "blocks) of the M1 candles before that time into 3-day paths starting at the fill; headline with the recent drift removed."),
    }
