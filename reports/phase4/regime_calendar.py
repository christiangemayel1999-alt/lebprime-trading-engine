"""2026 bullish/bearish calendar per symbol: H4 EMA50 slope (same definition as the engine's h4_trend)."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
import pandas as pd
from phase4_search import _frame
rows = []
for ds in (52, 53, 54, 55, 56, 57):
    meta, fr = _frame(ds, "2025-09-01..2026-11-21")
    f = fr.copy(); f["t"] = pd.to_datetime(f.timestamp_utc, utc=True)
    h4 = f.set_index("t").close.resample("4h").last().dropna()
    ema = h4.ewm(span=50, adjust=False).mean()
    trend = (ema.diff() > 0).map({True: "Bullish", False: "Bearish"})
    trend = trend[trend.index >= "2026-01-01"]
    m = trend.groupby(trend.index.strftime("%Y-%m")).apply(lambda s: (s == "Bullish").mean() * 100)
    for month, pct in m.items():
        rows.append({"symbol": meta["symbol"], "month": month, "bullish_pct": round(pct), "regime": "Bullish" if pct >= 60 else ("Bearish" if pct <= 40 else "Mixed")})
t = pd.DataFrame(rows)
t.to_csv("LebPrime AI Research/02 - Backtest Results/regime_calendar_2026.csv", index=False)
print(t.pivot(index="month", columns="symbol", values="regime").to_string())
