"""Winner vs loser by zone quality at creation (LIMIT) and at fill (Entry 1), train vs check years.

Usage: python scripts/phase4_zone_quality.py reports/phase4/m15_trades/trades.csv
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

t = pd.read_csv(sys.argv[1], low_memory=False)
t["train"] = t.year.astype(int) <= 2023
side = np.where(t.side == "BUY", 1.0, -1.0)
for m in ("limit", "entry1"):
    t[f"{m}_rsi_toward"] = np.where(t.side == "BUY", t[f"{m}_rsi14"], 100 - t[f"{m}_rsi14"])
    t[f"{m}_di_with"] = (t[f"{m}_plus_di14"] - t[f"{m}_minus_di14"]) * side
    t[f"{m}_macd_with"] = t[f"{m}_macd_hist"] / t[f"{m}_atr14"] * side
    t[f"{m}_ema200_with"] = t[f"{m}_ema200_dist_atr"] * side
    t[f"{m}_ema50_slope_with"] = t[f"{m}_ema50_slope_atr"] * side
    t[f"{m}_h4_align"] = np.where(t[f"{m}_h4_trend"].isna(), None, np.where(((t.side == "BUY") & (t[f"{m}_h4_trend"] == "UP")) | ((t.side == "SELL") & (t[f"{m}_h4_trend"] == "DOWN")), "WITH", "AGAINST"))
t["loss"] = t.state == "SL_HIT"
numeric = ["zone_width_atr", "risk_atr", "momentum_body_ratio", "departure_atr", "opposing_zone_r", "bars_source_to_limit", "base_bars",
           "limit_rsi_toward", "limit_adx14", "limit_di_with", "limit_macd_with", "limit_ema200_with", "limit_ema50_slope_with", "limit_atr14_percentile",
           "entry1_rsi_toward", "entry1_adx14", "entry1_di_with", "entry1_macd_with", "entry1_ema200_with", "entry1_atr14_percentile", "entry1_tick_volume_ratio"]
category = ["base_mode", "limit_h4_align", "entry1_h4_align", "limit_session", "symbol", "side"]
rows = []
for col in numeric + category:
    d = t[[col, "net_r", "result_r", "loss", "train"]].dropna(subset=[col]).copy()
    if d.empty:
        continue
    try:
        d["band"] = pd.qcut(d[col], 5, duplicates="drop") if col in numeric else d[col].astype(str)
    except ValueError:
        continue
    for band, g in d.groupby("band", observed=True):
        a, b = g[g.train], g[~g.train]
        if len(a) < 100 or len(b) < 60:
            continue
        rows.append({"value": col, "band": str(band), "tune_n": len(a), "tune_win": round((a.result_r > 0).mean() * 100, 1), "tune_sl%": round(a.loss.mean() * 100, 1),
                     "tune_avg": round(a.net_r.mean(), 3), "check_n": len(b), "check_win": round((b.result_r > 0).mean() * 100, 1), "check_sl%": round(b.loss.mean() * 100, 1),
                     "check_avg": round(b.net_r.mean(), 3)})
r = pd.DataFrame(rows)
base_a, base_b = t[t.train].net_r.mean(), t[~t.train].net_r.mean()
r["edge_both"] = np.minimum(r.tune_avg - base_a, r.check_avg - base_b)
r.to_csv(sys.argv[1].replace("trades.csv", "zone_quality.csv"), index=False)
pd.set_option("display.width", 250)
print(f"baseline avg net R: tune {base_a:+.3f}, check {base_b:+.3f}; SL share tune {t[t.train].loss.mean()*100:.1f}%")
print("\nBEST bands (better in both periods):\n", r.sort_values("edge_both", ascending=False).head(15).to_string(index=False))
print("\nWORST bands (worse in both periods):\n", r.assign(w=np.maximum(r.tune_avg - base_a, r.check_avg - base_b)).sort_values("w").head(12).drop(columns="w").to_string(index=False))
