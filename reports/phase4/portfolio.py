"""Continuous 2020-2026 trade lists for the passing settings and a combined portfolio, year by year."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
def run(job):
    label, sym, tf, ds, ov = job
    if ov.get("execution_model", "Live broker") != "Live broker":
        raise ValueError(f"{label}: only real-broker scoring is allowed (got {ov.get('execution_model')})")
    from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    meta, fr = _frame(ds, "2020-01-01..2026-11-22")
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ov}, symbol=sym, timeframe=tf).run(fr)
    single = ov.get("entry_mode") in ("Close confirmation", "Sweep reclaim", "Rejection candle", "Break of structure") or ov.get("entry2_mode") == "Off"
    rows = []
    for z in res["zones"]:
        if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < "2020-01-01": continue
        risk = abs(float(z.entry_price) - float(z.sl_price)); filled = sum(1 for l in (z.metadata.get("live_legs") or {}).values() if l.get("filled")) or 1
        w = 1.0 if (filled == 1 and single) else 0.5
        cost = (float(z.metadata.get("entry1_spread") or 0) * POINT.get(sym, 0) / risk) * w * filled if risk else 0
        v = VALUE_PER_PRICE_PER_LOT.get(sym, 0); cost += (COMMISSION_PER_LOT.get(sym, 0) / (risk * v)) * w * filled if risk and v else 0
        rows.append({"setting": label, "side": "BUY" if z.side == "DEMAND" else "SELL", "exit_time": z.terminal_time, "limit_time": z.limit_created_time, "entry_time": z.entry1_activated_time, "risk_price": risk, "net_r": float(z.result_r or 0) - cost})
    return rows
if __name__ == "__main__":
    rules = json.load(open("reports/phase4/regime_2026/rules.json")); best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv")
    pick = lambda s, tf, reg: rules[int(best[(best.symbol == s) & (best.tf == tf) & (best.regime == reg)].rule.iloc[0])]
    tr = json.load(open("reports/phase4/rules_trader.json")); trn = json.load(open("reports/phase4/rules_trader_names.json"))
    us30 = {**tr[trn.index("Sweep reclaim | H4+base | single TP 2R | buffer 0.3 ATR")], "limit_expiry_bars": 2016}
    jobs = [("USTEC M30 sweep sells", "USTEC", "M30", 61, pick("USTEC", "M30", "All")), ("Silver M30 uptrend buys", "XAGUSD", "M30", 59, pick("XAGUSD", "M30", "Bullish only")),
            ("US30 M5 sweep", "US30", "M5", 132, us30)]
    out = []
    with ProcessPoolExecutor(3) as p:
        for r in p.map(run, jobs): out += r
    B = "LebPrime AI Research/02 - Backtest Results"
    for f, lab in (("G1buys_exp8640_gold_M5_trades_2020_2026.csv", "Gold M5 G1B"), ("G3_gold_M15_trades_2020_2026.csv", "Gold M15 G3")):
        t = pd.read_csv(f"{B}/{f}"); out += [{"setting": lab, "side": s, "exit_time": e, "net_r": n} for s, e, n in zip(t.side, t.exit_time, t.net_r)]
    t = pd.DataFrame(out).sort_values("exit_time"); t.to_csv(f"{B}/portfolio_trades_2020_2026.csv", index=False)
    t["year"] = t.exit_time.str[:4]
    def stats(x):
        eq = x.net_r.cumsum(); return f"{(x.net_r > 0).mean()*100:.0f}% {x.net_r.sum():+.1f}R ({len(x)} tr, DD {(eq.cummax() - eq).max():.1f}R)"
    print("| Setting | " + " | ".join(str(y) for y in range(2020, 2027)) + " | Total |")
    for lab, g in list(t.groupby("setting")) + [("PORTFOLIO", t)]:
        print(f"| {lab} | " + " | ".join(stats(g[g.year == str(y)]) if (g.year == str(y)).any() else "–" for y in range(2020, 2027)) + f" | {stats(g)} |")
    months = (pd.to_datetime(t.exit_time.max()) - pd.to_datetime(t.exit_time.min())).days / 30.4
    print("trades per month", round(len(t) / months, 1))
