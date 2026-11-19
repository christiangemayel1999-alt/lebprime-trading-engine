"""Phase 4 parameter search on frozen Lab datasets.

Stage A (sensitivity): change one parameter at a time from the defaults, training years only.
    python scripts/phase4_search.py sensitivity --datasets 52 ... 63 [--until 2024-01-01]
Stage B (random): random combinations from SPACE, one shared setting scored on all datasets.
    python scripts/phase4_search.py random --trials 300 --datasets 52 ... 63

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
# Round-trip commission per 1.0 lot (USD) and USD value of a 1.0 price move per 1.0 lot.
# XAUUSD 7.0 and USTEC 0.70 are measured on the demo account ($0.07 per 0.01 / 0.1 lot);
# the other symbols use the same rate as their asset class (metals/FX/oil 7.0, indices 0.70).
COMMISSION_PER_LOT = {"XAUUSD": 7.0, "XAGUSD": 7.0, "US30": 0.70, "USTEC": 0.70, "WTI": 7.0, "EURUSD": 7.0}
VALUE_PER_PRICE_PER_LOT = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0, "USTEC": 1.0, "WTI": 1000.0, "EURUSD": 100000.0}

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

SPACE = {
    "body_mult": [0.4, 0.5, 0.6, 0.7, 0.8, 1.0],
    "momentum_count": [3, 4, 5, 6],
    "min_zone_width_atr": [0.1, 0.2, 0.3, 0.4],
    "zone_refinement": ["Smallest Range Candle", "Full Base (Boundary 9)"],
    "compact_range_atr": [0.6, 0.9, 1.2],
    "zone_confirm_delay": [0, 3, 5],
    "min_bars_after_source": [10, 20],
    "stop_rule": ["Candle close (Pine)", "Price touch"],
    "hard_stop_beyond_r": [0.5, 1.0, 2.0],
    "tp1_r": [0.75, 1.0, 1.5],
    "tp2_r": [1.5, 2.0, 3.0, 4.0],
    "tp1_close_fraction": [0.0, 0.5, 1.0],
    "max_risk_atr": [0.0, 1.5, 2.5, 4.0],
    "trend_filter": ["None", "With EMA200", "Against EMA200"],
    "session_filter": ["All", "London + New York"],
}

_CACHE: dict[int, tuple[dict, pd.DataFrame]] = {}


def _frame(dataset_id: int, until: str | None) -> tuple[dict, pd.DataFrame]:
    """``until`` is "END" or "START..END" (ISO dates, START inclusive, END exclusive)."""
    key = (dataset_id, until)
    if key not in _CACHE:
        from services.database import DatabaseService
        from services.indicator_lab.dataset_service import IndicatorDatasetService
        from utils import load_runtime_config

        config = load_runtime_config(ROOT, require_mt5_credentials=False)
        service = IndicatorDatasetService(ROOT, DatabaseService(ROOT / config["storage"]["database_path"]), config)
        meta, frame = service.get_dataset(dataset_id), service.load_frame(dataset_id)
        if until:
            start, _, end = until.rpartition("..")
            # Keep 2000 warm-up candles before START so indicators and open zones are settled.
            mask_end = frame["timestamp_utc"] < end
            if start:
                first = frame.index[frame["timestamp_utc"] >= start]
                begin = max(0, int(first[0]) - 2000) if len(first) else len(frame)
                frame = frame[mask_end].iloc[begin:]
            else:
                frame = frame[mask_end]
            frame = frame.reset_index(drop=True)
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
    window_start = until.rpartition("..")[0] if until else ""
    for zone in result["zones"]:
        if zone.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT"}:
            continue
        if window_start and str(zone.limit_created_time) < window_start:
            continue
        risk = abs(float(zone.entry_price) - float(zone.sl_price))
        filled = sum(1 for leg in (zone.metadata.get("live_legs") or {}).values() if leg.get("filled")) or 1
        weight = 1.0 if filled == 1 and (overrides.get("entry2_mode") == "Off" or overrides.get("entry_mode") == "Close confirmation") else 0.5
        spread_r = (float(zone.metadata.get("entry1_spread") or 0.0) * point / risk) * weight * filled if risk else 0.0
        value = VALUE_PER_PRICE_PER_LOT.get(meta["symbol"], 0.0)
        commission_r = (COMMISSION_PER_LOT.get(meta["symbol"], 0.0) / (risk * value)) * weight * filled if risk and value else 0.0
        cost = spread_r + commission_r
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
    parser.add_argument("stage", choices=["sensitivity", "random", "evaluate"])
    parser.add_argument("--window", help="evaluate: START..END, e.g. 2024-01-01..2025-01-01")
    parser.add_argument("--top", type=int, default=20, help="evaluate: best N settings from stage_b_pooled.csv")
    parser.add_argument("--settings", help="evaluate: JSON file with a list of settings instead of --top")
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--datasets", type=int, nargs="+", required=True)
    parser.add_argument("--until", default="2024-01-01", help="Training cut-off (exclusive)")
    parser.add_argument("--out", default=str(ROOT / "reports" / "phase4"))
    parser.add_argument("--workers", type=int, default=7)
    args = parser.parse_args()
    if args.stage == "random":
        return random_stage(args)
    if args.stage == "evaluate":
        return evaluate_stage(args)
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


def random_stage(args) -> int:
    import random

    rng = random.Random(args.seed)
    trials: list[dict] = [{}]
    seen = {"{}"}
    while len(trials) < args.trials:
        trial = {name: rng.choice(values) for name, values in SPACE.items()}
        if trial["tp2_r"] <= trial["tp1_r"]:
            continue
        key = json.dumps(trial, sort_keys=True)
        if key not in seen:
            seen.add(key)
            trials.append(trial)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(d, t, args.until) for t in trials for d in args.datasets]
    rows = []
    previous = out / "stage_b_runs.csv"
    if previous.exists():
        # Resume: keep finished runs and only run what is missing (same seed => same trial list).
        done = pd.read_csv(previous)
        rows = done.to_dict("records")
        finished = {(int(r["dataset_id"]), r["overrides"]) for r in rows}
        jobs = [job for job in jobs if (job[0], json.dumps(job[1], sort_keys=True)) not in finished]
    print(f"{len(jobs)} runs to do ({len(trials)} trials x {len(args.datasets)} datasets, {len(rows)} already done)", flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, row in enumerate(pool.map(score, jobs, chunksize=2), start=1):
            rows.append(row)
            if i % (len(args.datasets) * 5) == 0:
                pd.DataFrame(rows).to_csv(out / "stage_b_runs.csv", index=False)
                print(f"  {i}/{len(jobs)} runs", flush=True)
    table = pd.DataFrame(rows)
    table.to_csv(out / "stage_b_runs.csv", index=False)
    pooled = table.groupby("overrides").agg(
        trades=("trades", "sum"), net_r=("net_r", "sum"),
        win_rate=("win_rate", "mean"), worst_context_r=("net_r", "min"),
        profitable_contexts=("net_r", lambda s: int((s > 0).sum())), max_dd_r=("max_dd_r", "max"),
    )
    pooled["avg_net_r"] = pooled.net_r / pooled.trades.clip(lower=1)
    pooled = pooled[pooled.trades >= 1000].sort_values("avg_net_r", ascending=False)
    pooled.to_csv(out / "stage_b_pooled.csv")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 400)
    print(pooled.head(15).to_string())
    return 0


def evaluate_stage(args) -> int:
    out = Path(args.out)
    if args.settings:
        settings = [json.dumps(item, sort_keys=True) for item in json.loads(Path(args.settings).read_text(encoding="utf-8"))]
    else:
        pooled = pd.read_csv(out / "stage_b_pooled.csv")
        settings = ["{}"] + [o for o in pooled.overrides.head(args.top) if o != "{}"]
    jobs = [(d, json.loads(o), args.window) for o in settings for d in args.datasets]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(score, jobs, chunksize=2))
    table = pd.DataFrame(rows)
    tag = args.window.replace("..", "_to_")
    table.to_csv(out / f"evaluate_{tag}.csv", index=False)
    pooled = table.groupby("overrides", sort=False).agg(
        trades=("trades", "sum"), net_r=("net_r", "sum"), win_rate=("win_rate", "mean"),
        profitable_contexts=("net_r", lambda s: int((s > 0).sum())), worst_context_r=("net_r", "min"), max_dd_r=("max_dd_r", "max"),
    )
    pooled["avg_net_r"] = pooled.net_r / pooled.trades.clip(lower=1)
    pooled.to_csv(out / f"evaluate_{tag}_pooled.csv")
    pd.set_option("display.width", 250)
    print(pooled.reset_index(drop=True).round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
