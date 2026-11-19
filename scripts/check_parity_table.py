"""Check the Python engine against the "Parity log" table of the parity check build.

For TradingView plans without "Export chart data":
  1. Add docs/indicator_reference/LebPrime_Signals_v3_4_1_parity_check.pine to the chart.
     A "Parity log" table (bottom right) lists the latest signals in UTC.
  2. Copy the rows into a CSV with the header
        created_utc,side,entry1,sl,tp1,tp2,status
     e.g. 2026-11-18 14:15,BUY,4071.25,4062.10,4080.40,4089.55,TP1 FINAL 2026-11-18 16:30
  3. Freeze the same symbol/timeframe candles as an Indicator Lab dataset (MT5, read-only),
     covering at least a few days before the oldest row.
  4. python scripts/check_parity_table.py --signals rows.csv --dataset-id 12 --symbol XAUUSD

Each TradingView row is matched to the Python LIMIT CREATED with the same side and candle
time, then levels and status are compared. TradingView and MT5 quotes can differ slightly
by broker feed; --tolerance sets the allowed price difference.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.indicator_lab.lebprime_engine import EXECUTION_PINE, LebPrimeEngine  # noqa: E402
from services.indicator_lab.parameter_registry import default_parameters  # noqa: E402

STATUS_BY_STATE = {"TP2_HIT": "TP2", "TP1_FINAL": "TP1 FINAL", "SL_HIT": "SL", "CANCELED": "CANCELED"}


def python_status(zone) -> str:
    if zone.terminal_state:
        return STATUS_BY_STATE.get(zone.terminal_state, zone.terminal_state)
    if zone.tp1_hit_bar_index is not None:
        return "TP1"
    if zone.entry2_activated_bar_index is not None:
        return "E1+E2"
    if zone.activated_bar_index is not None:
        return "E1"
    return "PENDING"


def load_candles(dataset_id: int | None, candles_csv: str | None) -> pd.DataFrame:
    if candles_csv:
        return pd.read_csv(candles_csv)
    from services.database import DatabaseService
    from services.indicator_lab.dataset_service import IndicatorDatasetService
    from utils import load_runtime_config

    config = load_runtime_config(ROOT, require_mt5_credentials=False)
    database = DatabaseService(ROOT / config["storage"]["database_path"])
    return IndicatorDatasetService(ROOT, database, config).load_frame(int(dataset_id))


def compare(signals: pd.DataFrame, candles: pd.DataFrame, *, symbol: str, timeframe: str, tolerance: float) -> pd.DataFrame:
    params = {**default_parameters("LEBPRIME"), "execution_model": EXECUTION_PINE}
    result = LebPrimeEngine(params, symbol=symbol, timeframe=timeframe).run(candles)
    zones = [z for z in result["zones"] if z.limit_created_bar_index is not None]
    by_key = {(pd.Timestamp(z.limit_created_time).tz_convert("UTC").strftime("%Y-%m-%d %H:%M"), "BUY" if z.side == "DEMAND" else "SELL"): z for z in zones}
    rows = []
    for _, row in signals.iterrows():
        key = (pd.Timestamp(str(row["created_utc"])).strftime("%Y-%m-%d %H:%M"), str(row["side"]).strip().upper())
        zone = by_key.get(key)
        item = {"created_utc": key[0], "side": key[1], "tv_status": str(row.get("status", "")).strip()}
        if zone is None:
            item.update({"match": "MISSING_IN_PYTHON"})
            rows.append(item)
            continue
        diffs = {}
        for col, value in (("entry1", zone.entry_price), ("sl", zone.sl_price), ("tp1", zone.tp1_price), ("tp2", zone.tp2_price)):
            diffs[col] = float(value) - float(row[col])
            item[f"py_{col}"] = round(float(value), 5)
        tv_status_word = re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", item["tv_status"])
        item["py_status"] = python_status(zone)
        levels_ok = all(abs(d) <= tolerance for d in diffs.values())
        status_ok = item["py_status"].upper() == tv_status_word.upper()
        item["max_level_diff"] = round(max(abs(d) for d in diffs.values()), 5)
        item["match"] = "OK" if levels_ok and status_ok else ("LEVELS_DIFFER" if not levels_ok else "STATUS_DIFFERS")
        rows.append(item)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--signals", required=True, help="CSV typed from the Parity log table")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset-id", type=int, help="Indicator Lab dataset with the same candles")
    source.add_argument("--candles", help="Candle CSV with timestamp_utc,open,high,low,close")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--tolerance", type=float, default=0.0, help="Allowed price difference per level (feed differences)")
    args = parser.parse_args()

    signals = pd.read_csv(args.signals)
    report = compare(signals, load_candles(args.dataset_id, args.candles), symbol=args.symbol, timeframe=args.timeframe, tolerance=args.tolerance)
    pd.set_option("display.width", 200)
    print(report.to_string(index=False))
    ok = int((report["match"] == "OK").sum())
    print(f"\n{ok} of {len(report)} TradingView signals match the Python engine")
    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
