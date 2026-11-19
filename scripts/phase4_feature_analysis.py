"""Winner vs loser analysis of the values recorded at LIMIT CREATED (known when the order is placed).

Usage: python scripts/phase4_feature_analysis.py [reports/phase4/trades.csv]

For each value, trades are split into five equal-count bands (numbers) or groups (categories).
Each band shows trades, win rate and average net R in the training years (2020-2023) and,
separately, in 2024-2026, so a pattern only counts if it holds in both.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NUMERIC = ["rsi14", "macd_hist", "adx14", "plus_di14", "minus_di14", "atr14_percentile", "ema50_dist_atr", "ema200_dist_atr",
           "ema50_slope_atr", "bb_width_atr", "tick_volume_ratio"]
CATEGORY = ["session", "h1_trend", "h4_trend", "weekday", "macd_hist_rising"]


def add_derived(t: pd.DataFrame) -> pd.DataFrame:
    sign = np.where(t["side"] == "BUY", 1.0, -1.0)
    # Side-adjusted values: positive = in the trade's favour.
    t["limit_rsi_toward_side"] = np.where(t["side"] == "BUY", t["limit_rsi14"], 100 - t["limit_rsi14"])
    t["limit_macd_hist_with_side"] = t["limit_macd_hist"] * sign
    t["limit_ema200_with_side"] = t["limit_ema200_dist_atr"] * sign
    t["limit_ema50_slope_with_side"] = t["limit_ema50_slope_atr"] * sign
    t["limit_di_with_side"] = (t["limit_plus_di14"] - t["limit_minus_di14"]) * sign
    for tf in ("h1", "h4"):
        trend = t[f"limit_{tf}_trend"]
        t[f"limit_{tf}_alignment"] = np.where(trend.isna(), None, np.where(((t["side"] == "BUY") & (trend == "UP")) | ((t["side"] == "SELL") & (trend == "DOWN")), "WITH", "AGAINST"))
    return t


def band_table(t: pd.DataFrame, column: str, numeric: bool) -> pd.DataFrame:
    data = t[[column, "net_r", "result_r", "train"]].dropna(subset=[column]).copy()
    if data.empty:
        return pd.DataFrame()
    if numeric:
        try:
            data["band"] = pd.qcut(data[column], 5, duplicates="drop")
        except ValueError:
            return pd.DataFrame()
    else:
        data["band"] = data[column].astype(str)
    rows = []
    for band, group in data.groupby("band", observed=True):
        train, test = group[group.train], group[~group.train]
        rows.append({
            "value": column, "band": str(band),
            "train_trades": len(train), "train_win": round((train.result_r > 0).mean() * 100, 1) if len(train) else None,
            "train_avg_r": round(train.net_r.mean(), 3) if len(train) else None,
            "test_trades": len(test), "test_win": round((test.result_r > 0).mean() * 100, 1) if len(test) else None,
            "test_avg_r": round(test.net_r.mean(), 3) if len(test) else None,
        })
    return pd.DataFrame(rows)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "phase4" / "trades.csv"
    trades = add_derived(pd.read_csv(path))
    trades["train"] = trades["year"].astype(int) <= 2023
    tables = []
    for name in NUMERIC:
        tables.append(band_table(trades, f"limit_{name}", True))
    for name in ["limit_rsi_toward_side", "limit_macd_hist_with_side", "limit_ema200_with_side", "limit_ema50_slope_with_side", "limit_di_with_side", "risk_atr", "zone_width_atr"]:
        tables.append(band_table(trades, name, True))
    for name in CATEGORY:
        tables.append(band_table(trades, f"limit_{name}", False))
    for name in ["limit_h1_alignment", "limit_h4_alignment", "side", "timeframe", "symbol"]:
        tables.append(band_table(trades, name, False))
    report = pd.concat([table for table in tables if not table.empty], ignore_index=True)
    out = path.parent / "feature_bands.csv"
    report.to_csv(out, index=False)
    base_train = trades[trades.train].net_r.mean()
    base_test = trades[~trades.train].net_r.mean()
    print(f"Baseline avg net R: train {base_train:+.3f} ({trades.train.sum()} trades), test {base_test:+.3f} ({(~trades.train).sum()} trades)")
    # Bands that beat the baseline in BOTH periods by a margin, with enough trades.
    good = report[(report.train_trades >= 150) & (report.test_trades >= 100) & (report.train_avg_r > base_train + 0.05) & (report.test_avg_r > base_test + 0.05)]
    bad = report[(report.train_trades >= 150) & (report.test_trades >= 100) & (report.train_avg_r < base_train - 0.05) & (report.test_avg_r < base_test - 0.05)]
    pd.set_option("display.width", 220)
    print("\nBands better than average in both periods:\n", good.sort_values("train_avg_r", ascending=False).to_string(index=False))
    print("\nBands worse than average in both periods:\n", bad.sort_values("train_avg_r").to_string(index=False))
    print(f"\nFull table: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
