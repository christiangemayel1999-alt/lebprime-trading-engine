"""Buy vs sell breakdown, Jun-Nov 2026, current bot and bot with the two management fixes."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
W = "2026-06-01..2026-11-21"
VARIANTS = {"current": {}, "fixed": {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0}}

def run(job):
    ds, name = job
    from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    meta, fr = _frame(ds, W)
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **VARIANTS[name]}, symbol=meta["symbol"], timeframe=meta["timeframe"]).run(fr)
    rows = []
    for z in res["zones"]:
        if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < W[:10]:
            continue
        risk = abs(float(z.entry_price) - float(z.sl_price))
        filled = sum(1 for l in (z.metadata.get("live_legs") or {}).values() if l.get("filled")) or 1
        cost = (float(z.metadata.get("entry1_spread") or 0) * POINT.get(meta["symbol"], 0) / risk) * 0.5 * filled if risk else 0
        v = VALUE_PER_PRICE_PER_LOT.get(meta["symbol"], 0)
        cost += (COMMISSION_PER_LOT.get(meta["symbol"], 0) / (risk * v)) * 0.5 * filled if risk and v else 0
        m = z.metadata
        rows.append({"variant": name, "symbol": meta["symbol"], "tf": meta["timeframe"], "side": "BUY" if z.side == "DEMAND" else "SELL",
                     "net_r": float(z.result_r or 0) - cost, "h4": m.get("limit_h4_trend") or m.get("candidate_h4_trend"),
                     "ema200": m.get("limit_ema200_dist_atr"), "session": m.get("limit_session"), "month": str(z.terminal_time)[:7]})
    return rows

if __name__ == "__main__":
    ds = [124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 148, 149, 150, 151]
    jobs = [(d, v) for d in ds for v in VARIANTS]
    out = []
    with ProcessPoolExecutor(3) as p:
        for r in p.map(run, jobs):
            out += r
    pd.DataFrame(out).to_csv("reports/phase4/side_split_trades.csv", index=False)
    print("done", len(out))
