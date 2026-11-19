"""How far price reacts from each zone touch before the SL: MFE and MAE in R, per symbol and timeframe.

For every LIMIT whose Entry 1 is touched, walk the candles AFTER the fill candle (the fill candle's
own extremes are ambiguous) until the structural SL is touched by a wick or 300 candles pass:
  mfe_r  = best move in the trade's favour before the SL, in R from Entry 1
  mae_r  = worst move against (deepest point) before reaching +1R (or the SL)
Usage: python scripts/phase4_reaction_profile.py --datasets 124 ... [--window 2020-01-01..2024-01-01]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def profile(job: tuple[int, str]) -> list[dict]:
    dataset_id, window = job
    from scripts.phase4_search import _frame
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters

    meta, frame = _frame(dataset_id, window)
    start = window.rpartition("..")[0] if window else ""
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), "stop_rule": "Price touch", "entry2_mode": "Off"}, symbol=meta["symbol"], timeframe=meta["timeframe"])
    result = engine.run(frame)
    high, low = frame["high"].to_numpy(float), frame["low"].to_numpy(float)
    times = frame["timestamp_utc"].to_numpy()
    rows = []
    for z in result["zones"]:
        if z.entry1_activated_bar_index is None or (start and str(z.limit_created_time) < start):
            continue
        demand = z.side == "DEMAND"
        entry, stop = float(z.entry_price), float(z.sl_price)
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        i0 = int(z.entry1_activated_bar_index) + 1
        best, worst, worst_before_1r, reached_1r, stopped, bars = 0.0, 0.0, 0.0, False, False, 0
        for i in range(i0, min(i0 + 300, len(frame))):
            fav = (high[i] - entry) / risk if demand else (entry - low[i]) / risk
            adv = (entry - low[i]) / risk if demand else (high[i] - entry) / risk
            if adv >= 1.0:  # SL touched (assume the stop comes before the favourable extreme on that candle)
                worst = max(worst, adv)
                if not reached_1r:
                    worst_before_1r = max(worst_before_1r, adv)
                stopped = True
                bars = i - i0 + 1
                break
            best = max(best, fav)
            worst = max(worst, adv)
            if not reached_1r:
                worst_before_1r = max(worst_before_1r, adv)
            if best >= 1.0:
                reached_1r = True
        rows.append({
            "symbol": meta["symbol"], "timeframe": meta["timeframe"], "side": "BUY" if demand else "SELL",
            "fill_time": str(times[int(z.entry1_activated_bar_index)]), "mfe_r": round(best, 3), "mae_before_1r": round(worst_before_1r, 3),
            "stopped": stopped, "bars": bars,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", type=int, nargs="+", required=True)
    parser.add_argument("--window", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = [r for part in pool.map(profile, [(d, args.window) for d in args.datasets]) for r in part]
    t = pd.DataFrame(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(out, index=False)
    levels = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    summary = []
    for (sym, tf), g in t.groupby(["symbol", "timeframe"]):
        row = {"symbol": sym, "timeframe": tf, "touches": len(g), "mfe_median": round(g.mfe_r.median(), 2)}
        for lvl in levels:
            row[f">={lvl}R"] = round((g.mfe_r >= lvl).mean() * 100, 1)
        row["mae_p50"] = round(g.mae_before_1r.median(), 2)
        row["mae_p80"] = round(g.mae_before_1r.quantile(0.8), 2)
        summary.append(row)
    s = pd.DataFrame(summary)
    s.to_csv(out.with_name(out.stem + "_summary.csv"), index=False)
    pd.set_option("display.width", 250)
    print(s.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
