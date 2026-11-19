"""Phase 4 parameter search on frozen Lab datasets.

Stage A (sensitivity): change one parameter at a time from the defaults, training years only.
    python scripts/phase4_search.py sensitivity --datasets 52 ... 63 [--until 2024-01-01]

Scores are in R after spread (same cost model as phase4_baseline.py). Live broker execution model,
candle-close SL, 2R emergency stop and equal lots, i.e. what the demo bot trades.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POINT = {"XAUUSD": 0.01, "XAGUSD": 0.001, "US30": 0.1, "USTEC": 0.1, "WTI": 0.001, "EURUSD": 0.00001}

GRID = {
    "body_mult": [0.3, 0.4, 0.6, 0.7, 0.8, 1.0],
    "momentum_count": [2, 3, 5, 6],
    "body_len": [10, 30],
    "leg_reset_mode": ["Any opposite close"],
    "compact_search_bars": [3, 8, 10],
    "compression_body_mult": [0.4, 0.8],
    "compact_range_atr": [0.6, 1.2],
    "compact_base_min_bars": [1, 3],
    "zone_refinement": ["Full Base (Boundary 9)", "Last Opposing Candle", "Extreme Wick + Tight Body"],
    "max_zone_width_atr": [1.0, 2.0, 2.5],
    "min_zone_width_atr": [0.05, 0.2, 0.3],
    "atr_len": [14, 30],
    "zone_confirm_delay": [0, 3, 10],
    "min_bars_after_source": [20, 30],
    "min_same_side_gap": [10, 20],
    "invalidation": ["Wick"],
    "allow_overlap": [True],
    "hard_stop_beyond_r": [0.5, 2.0],
    "stop_rule": ["Price touch"],
}

_CACHE: dict[int, tuple[dict, pd.DataFrame]] = {}


def _frame(dataset_id: int, until: str | None) -> tuple[dict, pd.DataFrame]:
    key = dataset_id
    if key not in _CACHE:
        from services.database import DatabaseService
        from services.indicator_lab.dataset_service import IndicatorDatasetService
        from utils import load_runtime_config

        config = load_runtime_config(ROOT, require_mt5_credentials=False)
        service = IndicatorDatasetService(ROOT, DatabaseService(ROOT / config["storage"]["database_path"]), config)
        meta, frame = service.get_dataset(dataset_id), service.load_frame(dataset_id)
        if until:
            frame = frame[frame["timestamp_utc"] < until].reset_index(drop=True)
        _CACHE[key] = (meta, frame)
    return _CACHE[key]


def score(job: tuple[int, dict, str | None]) -> dict:
    dataset_id, overrides, until = job
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters

    meta, frame = _frame(dataset_id, until)
    result = LebPrimeEngine({**default_parameters("LEBPRIME"), **overrides}, symbol=meta["symbol"], timeframe=meta["timeframe"]).run(frame)
    point = POINT.get(meta["symbol"], 0.0)
    nets, grosses, years = [], [], {}
    for zone in result["zones"]:
        if zone.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT"}:
            continue
        risk = abs(float(zone.entry_price) - float(zone.sl_price))
        filled = sum(1 for leg in (zone.metadata.get("live_legs") or {}).values() if leg.get("filled")) or 1
        cost = (float(zone.metadata.get("entry1_spread") or 0.0) * point / risk) * 0.5 * filled if risk else 0.0
        gross = float(zone.result_r or 0.0)
        nets.append(gross - cost)
        grosses.append(gross)
        year = str(zone.terminal_time)[:4]
        years[year] = years.get(year, 0.0) + gross - cost
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for value in nets:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    n = len(nets)
    return {
        "dataset_id": dataset_id, "symbol": meta["symbol"], "timeframe": meta["timeframe"],
        "overrides": json.dumps(overrides, sort_keys=True),
        "trades": n, "win_rate": round(sum(1 for g in grosses if g > 0) / n * 100, 2) if n else 0.0,
        "net_r": round(sum(nets), 2), "avg_net_r": round(sum(nets) / n, 4) if n else 0.0, "max_dd_r": round(max_dd, 2),
        "profitable_years": sum(1 for v in years.values() if v > 0), "years": len(years),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["sensitivity"])
    parser.add_argument("--datasets", type=int, nargs="+", required=True)
    parser.add_argument("--until", default="2024-01-01", help="Training cut-off (exclusive)")
    parser.add_argument("--out", default=str(ROOT / "reports" / "phase4"))
    parser.add_argument("--workers", type=int, default=7)
    args = parser.parse_args()
    variants = [{}] + [{name: value} for name, values in GRID.items() for value in values]
    jobs = [(d, v, args.until) for d, v in itertools.product(args.datasets, variants)]
    print(f"{len(jobs)} runs ({len(variants)} variants x {len(args.datasets)} datasets)", flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(score, jobs, chunksize=4))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(rows)
    table.to_csv(out / "stage_a_sensitivity.csv", index=False)
    base = table[table.overrides == "{}"].set_index(["symbol", "timeframe"])["avg_net_r"]
    table["delta_vs_default"] = table.apply(lambda r: r.avg_net_r - base.get((r.symbol, r.timeframe), 0.0), axis=1)
    pooled = table.groupby("overrides").agg(trades=("trades", "sum"), net_r=("net_r", "sum"), contexts_better=("delta_vs_default", lambda s: int((s > 0).sum())), mean_delta=("delta_vs_default", "mean")).sort_values("net_r", ascending=False)
    pooled.to_csv(out / "stage_a_pooled.csv")
    pd.set_option("display.width", 200)
    print(pooled.head(25).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
