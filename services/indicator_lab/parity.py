"""Bar-by-bar comparison of the Python LebPrime engine with a TradingView chart export.

Workflow:
  1. Add docs/indicator_reference/LebPrime_Signals_v3_4_1_parity_export.pine to a
     TradingView chart, set "Recent history bars" to cover the whole chart (e.g. 5000).
  2. Export chart data (CSV). The file holds the candles plus 26 "LP <side> <event>" columns.
  3. Run scripts/run_pine_parity.py on the CSV. The engine runs in Pine mode on the very
     same candles and every event slot is compared.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from services.indicator_lab.lebprime_engine import EXECUTION_PINE, LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters

SLOT_NAMES = ["CREATED", "TOP", "BOTTOM", "SL", "TP1", "TP2", "ENTRY1", "ENTRY2", "TP1_HIT", "TP2_HIT", "TP1_FINAL", "SL_HIT", "CANCELED"]
EVENT_SLOTS = {
    "ENTRY1_ACTIVATED": "ENTRY1",
    "ENTRY2_ACTIVATED": "ENTRY2",
    "TP1_HIT": "TP1_HIT",
    "TP2_HIT": "TP2_HIT",
    "TP1_FINAL": "TP1_FINAL",
    "SL_HIT": "SL_HIT",
    "CANCELED": "CANCELED",
}


def column_name(side: str, slot: str) -> str:
    return f"LP {side} {slot}"


def load_tradingview_export(path: str | Path) -> pd.DataFrame:
    """Read a TradingView "Export chart data" CSV into engine candles plus parity columns."""
    frame = pd.read_csv(path)
    lower = {str(col).strip().lower(): col for col in frame.columns}
    for required in ("time", "open", "high", "low", "close"):
        if required not in lower:
            raise ValueError(f"TradingView export is missing the '{required}' column")
    raw_time = frame[lower["time"]]
    if pd.api.types.is_numeric_dtype(raw_time):
        times = pd.to_datetime(raw_time, unit="s", utc=True)
    else:
        times = pd.to_datetime(raw_time, utc=True)
    out = pd.DataFrame(
        {
            "timestamp_utc": times.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "open": pd.to_numeric(frame[lower["open"]], errors="coerce"),
            "high": pd.to_numeric(frame[lower["high"]], errors="coerce"),
            "low": pd.to_numeric(frame[lower["low"]], errors="coerce"),
            "close": pd.to_numeric(frame[lower["close"]], errors="coerce"),
        }
    )
    missing = [column_name(side, slot) for side in ("B", "S") for slot in SLOT_NAMES if column_name(side, slot) not in frame.columns]
    if missing:
        raise ValueError(
            "TradingView export has no parity columns (e.g. 'LP B CREATED'). Use the parity export build of the "
            f"indicator. Missing: {missing[:4]}..."
        )
    for side in ("B", "S"):
        for slot in SLOT_NAMES:
            out[column_name(side, slot)] = pd.to_numeric(frame[column_name(side, slot)], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def python_event_grid(frame: pd.DataFrame, *, symbol: str, timeframe: str, history_bars: int, overrides: dict[str, Any] | None = None) -> pd.DataFrame:
    """Run the engine in Pine mode and lay its events out in the same per-bar slots as the Pine export."""
    params = {**default_parameters("LEBPRIME"), **(overrides or {}), "execution_model": EXECUTION_PINE, "apply_history_window": True, "history_bars": int(history_bars)}
    engine = LebPrimeEngine(params, symbol=symbol, timeframe=timeframe)
    candles = frame[["timestamp_utc", "open", "high", "low", "close"]].copy()
    result = engine.run(candles)
    zones = {zone.engine_zone_id: zone for zone in result["zones"]}
    grid = pd.DataFrame(index=frame.index, columns=[column_name(s, slot) for s in ("B", "S") for slot in SLOT_NAMES], dtype=float)
    for event in result["events"]:
        zone = zones.get(event.zone_engine_id)
        if zone is None:
            continue
        side = "B" if zone.side == "DEMAND" else "S"
        row = int(event.bar_index)
        if event.event_type == "LIMIT_CREATED":
            for slot, value in (("CREATED", zone.entry_price), ("TOP", zone.zone_top), ("BOTTOM", zone.zone_bottom), ("SL", zone.sl_price), ("TP1", zone.tp1_price), ("TP2", zone.tp2_price)):
                grid.at[row, column_name(side, slot)] = float(value)
            continue
        slot = EVENT_SLOTS.get(event.event_type)
        if slot is None:
            continue
        price = event.price
        if price is None:
            price = zone.entry_price
        grid.at[row, column_name(side, slot)] = float(price)
    return grid


def _same(a: float, b: float, tolerance: float) -> bool:
    a_na = a is None or (isinstance(a, float) and math.isnan(a))
    b_na = b is None or (isinstance(b, float) and math.isnan(b))
    if a_na or b_na:
        return a_na and b_na
    return abs(float(a) - float(b)) <= tolerance


def compare_tradingview_export(path: str | Path, *, symbol: str, timeframe: str = "M15", history_bars: int = 5000, tolerance: float | None = None, overrides: dict[str, Any] | None = None, max_examples: int = 50) -> dict[str, Any]:
    frame = load_tradingview_export(path)
    if tolerance is None:
        # Half of the smallest price step seen in the candles.
        steps = pd.concat([frame[c].diff().abs() for c in ("open", "high", "low", "close")])
        steps = steps[steps > 0]
        tolerance = float(steps.min()) / 2.0 if not steps.empty else 1e-9
    grid = python_event_grid(frame, symbol=symbol, timeframe=timeframe, history_bars=history_bars, overrides=overrides)
    counts_tv: Counter = Counter()
    counts_py: Counter = Counter()
    matched: Counter = Counter()
    mismatches: list[dict[str, Any]] = []
    for col in grid.columns:
        for row in frame.index:
            tv = frame.at[row, col]
            py = grid.at[row, col]
            tv_has = not pd.isna(tv)
            py_has = not pd.isna(py)
            if tv_has:
                counts_tv[col] += 1
            if py_has:
                counts_py[col] += 1
            if not tv_has and not py_has:
                continue
            if _same(None if not tv_has else float(tv), None if not py_has else float(py), tolerance):
                matched[col] += 1
            elif len(mismatches) < max_examples:
                mismatches.append({"bar": int(row), "time": frame.at[row, "timestamp_utc"], "slot": col, "tradingview": None if not tv_has else float(tv), "python": None if not py_has else float(py)})
            else:
                mismatches.append(None)  # counted, not detailed
    total_events = sum(max(counts_tv[c], counts_py[c]) for c in grid.columns)
    mismatch_count = sum(1 for item in mismatches)
    return {
        "file": str(path),
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": int(len(frame)),
        "first_bar": frame["timestamp_utc"].iloc[0] if len(frame) else None,
        "last_bar": frame["timestamp_utc"].iloc[-1] if len(frame) else None,
        "tolerance": tolerance,
        "tradingview_events": dict(counts_tv),
        "python_events": dict(counts_py),
        "matched": dict(matched),
        "mismatch_count": mismatch_count,
        "match_rate": (1.0 - mismatch_count / total_events) if total_events else 1.0,
        "parity": mismatch_count == 0,
        "examples": [item for item in mismatches if item is not None],
    }
