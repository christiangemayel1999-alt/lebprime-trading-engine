"""Year-by-year win % and net R for each symbol/timeframe: uptrend setting + downtrend setting (2026 regime winners) run continuously."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
sys.path.insert(0, "reports/phase4")
DS = {}
for i, s in enumerate(["XAUUSD","XAGUSD","US30","USTEC","WTI","EURUSD"]):
    DS[(s,"H1")] = 52+i; DS[(s,"M30")] = 58+i; DS[(s,"M15")] = 124+i; DS[(s,"M5")] = 130+i; DS[(s,"M3")] = 136+i
for i, s in enumerate(["XAUUSD","XAGUSD","US30","USTEC"]):
    DS[(s,"M1")] = 148+i; DS[(s,"M2")] = 142+i
START = {"H1": "2020-01-01", "M30": "2020-01-01", "M15": "2020-01-01", "M5": "2020-01-01", "M3": "2021-01-01", "M2": "2022-01-01", "M1": "2023-01-01"}
def run(job):
    sym, tf, label, ov = job
    from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    win = f"{START[tf]}..2026-11-22"
    meta, fr = _frame(DS[(sym, tf)], win)
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ov}, symbol=sym, timeframe=tf).run(fr)
    single = ov.get("entry_mode") in ("Close confirmation", "Sweep reclaim", "Rejection candle", "Break of structure") or ov.get("entry2_mode") == "Off"
    rows = []
    for z in res["zones"]:
        if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < win[:10]: continue
        risk = abs(float(z.entry_price) - float(z.sl_price)); filled = sum(1 for l in (z.metadata.get("live_legs") or {}).values() if l.get("filled")) or 1
        w = 1.0 if (filled == 1 and single) else 0.5
        cost = (float(z.metadata.get("entry1_spread") or 0) * POINT.get(sym, 0) / risk) * w * filled if risk else 0
        v = VALUE_PER_PRICE_PER_LOT.get(sym, 0); cost += (COMMISSION_PER_LOT.get(sym, 0) / (risk * v)) * w * filled if risk and v else 0
        rows.append({"symbol": sym, "tf": tf, "setting": label, "side": "BUY" if z.side == "DEMAND" else "SELL", "year": str(z.terminal_time)[:4], "net_r": float(z.result_r or 0) - cost})
    return rows
if __name__ == "__main__":
    rules = json.load(open("reports/phase4/regime_2026/rules.json"))
    best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv")
    jobs = []
    for (sym, tf), g in best.groupby(["symbol", "tf"]):
        if (sym, tf) in (("XAUUSD", "M5"), ("XAUUSD", "M15")): continue
        for reg in ("Bullish only", "Bearish only"):
            r = g[g.regime == reg]
            if len(r): jobs.append((sym, tf, reg, rules[int(r.rule.iloc[0])]))
    out = []
    with ProcessPoolExecutor(7) as p:
        for rows in p.map(run, jobs):
            out += rows
            pd.DataFrame(out).to_csv("reports/phase4/yearly_trades.csv", index=False)
    print("ALL DONE", len(out), flush=True)
