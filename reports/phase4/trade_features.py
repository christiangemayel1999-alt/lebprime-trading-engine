"""Trade-level export with indicator values at the LIMIT and at the Entry 1 fill, for win/loss analysis."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
G1 = json.load(open("reports/phase4/rules_gold_final_M5.json"))[0]
FIXED = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0}
JOBS = [("G1_gold_M5", 130, G1, "2020-01-01..2026-11-21")] + [("fixed_M5", d, FIXED, "2025-11-20..2026-11-21") for d in (130, 131, 132, 133, 134, 135)] + \
       [("fixed_M1", d, FIXED, "2025-11-20..2026-11-21") for d in (148, 149, 150, 151)]
def run(job):
    name, ds, ov, win = job
    from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    meta, fr = _frame(ds, win)
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ov}, symbol=meta["symbol"], timeframe=meta["timeframe"]).run(fr)
    rows = []
    for z in res["zones"]:
        if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < win[:10]:
            continue
        risk = abs(float(z.entry_price) - float(z.sl_price))
        legs = z.metadata.get("live_legs") or {}
        filled = sum(1 for l in legs.values() if l.get("filled")) or 1
        sym = meta["symbol"]
        cost = float(z.metadata.get("entry1_spread") or 0) * POINT.get(sym, 0) / risk * 0.5 * filled if risk else 0
        v = VALUE_PER_PRICE_PER_LOT.get(sym, 0)
        cost += COMMISSION_PER_LOT.get(sym, 0) / (risk * v) * 0.5 * filled if risk and v else 0
        m = z.metadata
        r = {"test": name, "symbol": sym, "tf": meta["timeframe"], "side": "BUY" if z.side == "DEMAND" else "SELL", "state": z.terminal_state, "legs": filled,
             "net_r": float(z.result_r or 0) - cost, "limit_time": z.limit_created_time, "fill_time": z.entry1_activated_time,
             "fill_delay_bars": (z.entry1_activated_bar_index or 0) - (z.limit_created_bar_index or 0),
             "zone_width_atr": z.zone_width_atr, "risk_atr": risk / float(z.atr_at_source) if z.atr_at_source else None,
             "departure_atr": z.departure_distance_atr, "mfe_r": z.mfe_r, "mae_r": z.mae_r}
        for pre in ("limit", "entry1"):
            for k in ("rsi14", "adx14", "atr14_percentile", "ema200_dist_atr", "ema50_slope_atr", "h4_trend", "h1_trend", "session", "hour_utc", "bb_width_atr", "tick_volume_ratio", "macd_hist"):
                r[f"{pre}_{k}"] = m.get(f"{pre}_{k}")
        rows.append(r)
    return rows
if __name__ == "__main__":
    out = []
    with ProcessPoolExecutor(5) as p:
        for r in p.map(run, JOBS):
            out += r
    pd.DataFrame(out).to_csv("reports/phase4/trade_features.csv", index=False)
    print("done", len(out))
