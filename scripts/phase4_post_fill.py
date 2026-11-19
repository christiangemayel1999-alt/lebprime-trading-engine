"""What happens between the fill and the outcome: early move, candles spent inside the zone,
re-touches of the far edge and momentum against, for winners vs losers.

Usage: python scripts/phase4_post_fill.py reports/phase4/m15_trades/trades.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.phase4_search import _frame  # noqa: E402

t = pd.read_csv(sys.argv[1], low_memory=False)
rows = []
for dataset_id, group in t.groupby("dataset_id"):
    _, frame = _frame(int(dataset_id), None)
    o, h, l, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    for _, tr in group.iterrows():
        i0 = int(tr.fill_bar_index)
        demand = tr.side == "BUY"
        entry, sl, top, bot = tr.entry, tr.sl, tr.zone_top, tr.zone_bottom
        risk = abs(entry - sl)
        far = bot if demand else top
        win = slice(i0 + 1, min(i0 + 9, len(frame)))  # 8 candles after the fill candle
        hi, lo, cl, op = h[win], l[win], c[win], o[win]
        if len(cl) == 0 or risk <= 0:
            continue
        fav = ((hi - entry) if demand else (entry - lo)) / risk
        inside = ((cl <= top) & (cl >= bot)).sum()
        beyond_far = ((cl < far) if demand else (cl > far)).sum()
        against_bodies = ((cl < op) if demand else (cl > op))
        fill_close_r = ((c[i0] - entry) if demand else (entry - c[i0])) / risk
        rows.append({"state": tr.state, "net_r": tr.net_r, "train": int(tr.year) <= 2023,
                     "fill_close_r": fill_close_r, "mfe_3": fav[:3].max(), "mfe_8": fav.max(),
                     "closes_inside_8": inside, "closes_beyond_far_8": beyond_far, "against_candles_3": against_bodies[:3].sum()})
d = pd.DataFrame(rows)
d["outcome"] = np.where(d.state == "SL_HIT", "LOSS (SL)", "WIN/OTHER")
print(d.groupby("outcome")[["fill_close_r", "mfe_3", "mfe_8", "closes_inside_8", "closes_beyond_far_8", "against_candles_3"]].median().round(2).to_string())
for col, cuts in [("fill_close_r", [-9, -0.5, -0.25, 0, 0.25, 9]), ("mfe_3", [-1, 0.1, 0.25, 0.5, 1, 99]), ("closes_beyond_far_8", [-1, 0, 1, 2, 9])]:
    d["band"] = pd.cut(d[col], cuts)
    print(f"\n{col}:")
    print(d.groupby(["band"], observed=True).agg(n=("net_r", "size"), sl_pct=("state", lambda s: round((s == "SL_HIT").mean() * 100, 1)), avg_r=("net_r", "mean")).round(3).to_string())
d.to_csv(sys.argv[1].replace("trades.csv", "post_fill.csv"), index=False)
