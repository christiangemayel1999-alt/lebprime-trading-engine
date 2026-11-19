"""Phase 4 baseline: run the LebPrime engine on frozen Lab datasets and export every trade.

Usage:
    python scripts/phase4_baseline.py --datasets 22 23 24 25 26 27 40 41 42 43 44 45 [--out reports/phase4]

Writes <out>/trades.csv (one row per closed trade, with the recorded indicator values) and
<out>/summary.csv (per dataset and year). Spread cost in R = bar spread at the Entry 1 fill /
Entry 1 risk, weighted by the entries that filled (each entry carries half the risk).
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POINT = {"XAUUSD": 0.01, "XAGUSD": 0.001, "US30": 0.1, "USTEC": 0.1, "WTI": 0.001, "EURUSD": 0.00001}


def _run(args: tuple[int, dict]) -> list[dict]:
    dataset_id, overrides = args
    from services.database import DatabaseService
    from services.indicator_lab.dataset_service import IndicatorDatasetService
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    from utils import load_runtime_config

    config = load_runtime_config(ROOT, require_mt5_credentials=False)
    service = IndicatorDatasetService(ROOT, DatabaseService(ROOT / config["storage"]["database_path"]), config)
    meta = service.get_dataset(dataset_id)
    frame = service.load_frame(dataset_id)
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), **overrides}, symbol=meta["symbol"], timeframe=meta["timeframe"])
    result = engine.run(frame)
    point = POINT.get(meta["symbol"], 0.0)
    rows = []
    for zone in result["zones"]:
        if zone.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT"}:
            continue
        risk = abs(float(zone.entry_price) - float(zone.sl_price))
        legs = (zone.metadata.get("live_legs") or {})
        filled = sum(1 for leg in legs.values() if leg.get("filled")) or 1
        spread_points = zone.metadata.get("entry1_spread") or 0.0
        cost_r = (float(spread_points) * point / risk) * 0.5 * filled if risk else 0.0
        row = {
            "dataset_id": dataset_id, "symbol": meta["symbol"], "timeframe": meta["timeframe"],
            "zone_id": zone.engine_zone_id, "side": "BUY" if zone.side == "DEMAND" else "SELL",
            "limit_time": zone.limit_created_time, "exit_time": zone.terminal_time, "year": str(zone.terminal_time)[:4],
            "state": zone.terminal_state, "exit_reason": zone.metadata.get("exit_reason"),
            "risk_price": risk, "risk_atr": risk / float(zone.metadata.get("limit_atr14") or risk or 1.0),
            "zone_width_atr": zone.zone_width_atr, "entries_filled": filled,
            "result_r": float(zone.result_r or 0.0), "cost_r": cost_r, "net_r": float(zone.result_r or 0.0) - cost_r,
            "mfe_r": zone.mfe_r, "mae_r": zone.mae_r,
        }
        row.update({k: v for k, v in zone.metadata.items() if k.split("_", 1)[0] in {"candidate", "limit", "entry1", "exit"} and not isinstance(v, (dict, list))})
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", type=int, nargs="+", required=True)
    parser.add_argument("--out", default=str(ROOT / "reports" / "phase4"))
    parser.add_argument("--workers", type=int, default=7)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_run, [(d, {}) for d in args.datasets]))
    trades = pd.DataFrame([row for rows in results for row in rows])
    trades.to_csv(out / "trades.csv", index=False)
    summary = (
        trades.groupby(["symbol", "timeframe", "year"])
        .agg(trades=("net_r", "size"), win_rate=("result_r", lambda s: (s > 0).mean() * 100), gross_r=("result_r", "sum"), net_r=("net_r", "sum"))
        .round(2)
    )
    summary.to_csv(out / "summary.csv")
    total = trades.groupby(["symbol", "timeframe"]).agg(
        trades=("net_r", "size"), win_rate=("result_r", lambda s: (s > 0).mean() * 100),
        gross_r=("result_r", "sum"), cost_r=("cost_r", "sum"), net_r=("net_r", "sum"), avg_net_r=("net_r", "mean"),
    ).round(2)
    print(total.to_string())
    print(f"\nAll: {len(trades)} trades, win {(trades.result_r > 0).mean() * 100:.1f}%, gross {trades.result_r.sum():+.1f}R, net {trades.net_r.sum():+.1f}R")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
