"""Trade-level ledger for G1 on Gold M5, 2020-01 .. 2026-11-21 in one continuous run, with cost stress."""
import sys, os, json, hashlib
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
import pandas as pd
from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters
G1 = {**json.load(open("reports/phase4/rules_gold_final_M5.json"))[0], "side_filter": "Buys only", "limit_expiry_bars": 2016}
meta, fr = _frame(130, "2020-01-01..2026-11-21")
eng = LebPrimeEngine({**default_parameters("LEBPRIME"), **G1}, symbol="XAUUSD", timeframe="M5")
res = eng.run(fr)
rows = []
for z in res["zones"]:
    if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < "2020-01-01":
        continue
    risk = abs(float(z.entry_price) - float(z.sl_price))
    legs = z.metadata.get("live_legs") or {}
    filled = sum(1 for l in legs.values() if l.get("filled")) or 1
    spread_r = float(z.metadata.get("entry1_spread") or 0) * POINT["XAUUSD"] / risk * 0.5 * filled
    comm_r = COMMISSION_PER_LOT["XAUUSD"] / (risk * VALUE_PER_PRICE_PER_LOT["XAUUSD"]) * 0.5 * filled
    rows.append({"zone_id": z.engine_zone_id, "side": "BUY" if z.side == "DEMAND" else "SELL", "limit_time": z.limit_created_time,
                 "entry1_time": z.entry1_activated_time, "exit_time": z.terminal_time, "state": z.terminal_state, "legs_filled": filled,
                 "entry1": z.entry1, "entry2": z.entry2, "sl": z.sl_price, "tp1": z.tp1_price, "tp2": z.tp2_price, "risk_price": risk,
                 "spread_points": z.metadata.get("entry1_spread"), "gross_r": float(z.result_r or 0), "spread_r": spread_r, "commission_r": comm_r,
                 "net_r": float(z.result_r or 0) - spread_r - comm_r})
t = pd.DataFrame(rows).sort_values("exit_time")
OUT = "LebPrime AI Research/02 - Backtest Results"
t.to_csv(f"{OUT}/G1buys_exp2016_gold_M5_trades_2020_2026.csv", index=False)
def stats(x, extra=0.0, spread_mult=1.0):
    n = x.gross_r - x.spread_r * spread_mult - x.commission_r - extra
    w, l = n[n > 0], n[n <= 0]
    eq = n.cumsum(); dd = (eq.cummax() - eq).max()
    return {"trades": len(n), "win_pct": round(100 * len(w) / len(n), 1), "net_r": round(n.sum(), 1), "avg_r": round(n.mean(), 4),
            "avg_win": round(w.mean(), 3), "avg_loss": round(l.mean(), 3), "profit_factor": round(w.sum() / -l.sum(), 3), "max_dd_r": round(dd, 1)}
t["year"] = t.exit_time.str[:4]
out = {"code_commit": os.popen("git rev-parse --short HEAD").read().strip(), "engine_sha256": hashlib.sha256(open("services/indicator_lab/lebprime_engine.py", "rb").read()).hexdigest()[:16],
       "dataset_id": 130, "settings": G1, "all": stats(t), "spread_x1.5": stats(t, spread_mult=1.5), "slip_0.02R": stats(t, 0.02), "slip_0.05R": stats(t, 0.05),
       "by_year": {y: stats(g) for y, g in t.groupby("year")}, "by_side": {s: stats(g) for s, g in t.groupby("side")}, "by_legs": {str(k): stats(g) for k, g in t.groupby("legs_filled")}}
json.dump(out, open(f"{OUT}/G1buys_exp2016_gold_M5_summary.json", "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in out.items() if k != "settings"}, indent=1, default=str))
