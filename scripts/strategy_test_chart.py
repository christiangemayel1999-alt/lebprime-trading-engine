"""Chart files for a dashboard strategy test (display only).

Called by scripts/strategy_test.py inside the research sandbox, after the engine ran. For each symbol it
saves, next to trades.csv:

* chart_<SYM>.npy   candles of the tested timeframe from the test start: structured array
                    t (epoch seconds, int64), o, h, l, c (float32). The dashboard reads it memory-mapped.
* chart_<SYM>.json  every zone created from the test start, with what the engine did with it: box
                    (created -> ended, top/bottom), limit order time, entry fill, SL, TP, every stop move
                    of the trailing exit, the exit and its gross/net R (net = the trades.csv row).

Nothing here changes a trade or a result; it only writes down what the engine already produced.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CANDLE_DTYPE = np.dtype([("t", "<i8"), ("o", "<f4"), ("h", "<f4"), ("l", "<f4"), ("c", "<f4")])
CHART_VERSION = 1


def epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int((ts - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1))


def num(value: Any, digits: int = 6) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, digits) if math.isfinite(out) else None


def candles_array(frame: pd.DataFrame, start: str) -> np.ndarray:
    times = pd.to_datetime(frame["timestamp_utc"], utc=True)
    keep = (times >= pd.Timestamp(start[:10], tz="UTC")).to_numpy()
    sub = frame.loc[keep]
    out = np.empty(len(sub), dtype=CANDLE_DTYPE)
    out["t"] = ((times[keep] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy()
    for src, dst in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
        out[dst] = sub[src].to_numpy(dtype=np.float64)
    return out


def zone_records(result: dict, start: str, last_time: int | None, trade_rows: list[dict]) -> list[dict]:
    """One compact dict per zone created on or after ``start`` (the boxes the chart draws)."""
    net_by_zone = {row["zone_id"]: row for row in trade_rows}
    stops: dict[str, list[list[float]]] = {}
    for event in result.get("events") or []:
        if event.event_type == "STOP_MOVED" and event.price is not None:
            t = epoch(event.timestamp)
            if t is not None:
                stops.setdefault(event.zone_engine_id, []).append([t, num(event.price)])
    start_key = start[:10]
    out = []
    for z in result.get("zones") or []:
        if str(z.created_time or "")[:10] < start_key:
            continue
        created = epoch(z.created_time)
        if created is None:
            continue
        ended = epoch(z.terminal_time) or last_time
        row = net_by_zone.get(z.engine_zone_id)
        md = z.metadata or {}
        item = {
            "id": z.engine_zone_id, "side": "BUY" if z.side == "DEMAND" else "SELL",
            "created": created, "ended": ended, "state": z.terminal_state or "OPEN",
            "reason": z.terminal_reason, "top": num(z.zone_top), "bottom": num(z.zone_bottom),
            "limit": epoch(z.limit_created_time), "entry_t": epoch(z.entry1_activated_time),
            "entry2_t": epoch(z.entry2_activated_time), "entry": num(z.entry_price),
            "entry1": num(z.entry1), "entry2": num(z.entry2),
            "sl": num(z.sl_price), "tp": num(z.tp_price), "tp1": num(z.tp1_price), "tp2": num(z.tp2_price),
            "tp1_hit": epoch(z.tp1_hit_time), "exit_price": num(z.exit_price),
            "gross_r": num(z.result_r, 4), "mfe_r": num(z.mfe_r, 3), "mae_r": num(z.mae_r, 3),
        }
        for leg, key in ((1, "fill"), (2, "fill2")):
            value = md.get(f"entry{leg}_fill_price")
            if value is not None:
                item[key] = num(value)
        if z.engine_zone_id in stops:
            item["stops"] = stops[z.engine_zone_id]
        if row is not None:  # a trade counted in trades.csv / summary.json
            item["trade"] = True
            item["net_r"] = row["net_r"]
            item["cost_r"] = row["cost_r"]
        out.append({k: v for k, v in item.items() if v is not None})
    out.sort(key=lambda r: r["created"])
    return out


def write_chart(folder: Path, symbol: str, timeframe: str, frame: pd.DataFrame, result: dict, start: str,
                trade_rows: list[dict], trail: bool = False) -> dict:
    candles = candles_array(frame, start)
    last_time = int(candles["t"][-1]) if len(candles) else None
    zones = zone_records(result, start, last_time, trade_rows)
    np.save(folder / f"chart_{symbol}.npy", candles, allow_pickle=False)
    meta = {"version": CHART_VERSION, "symbol": symbol, "timeframe": timeframe, "bars": int(len(candles)),
            "exit_mode": "Trail" if trail else "Targets",
            "first": int(candles["t"][0]) if len(candles) else None, "last": last_time, "zones": zones}
    tmp = folder / f"chart_{symbol}.json.tmp"
    tmp.write_text(json.dumps(meta, separators=(",", ":")))
    tmp.replace(folder / f"chart_{symbol}.json")
    return {"bars": meta["bars"], "zones": len(zones), "trades": sum(1 for z in zones if z.get("trade"))}
