import pandas as pd, json, os
rules = json.load(open("reports/phase4/regime_2026/rules.json")); key = {json.dumps(r, sort_keys=True): i for i, r in enumerate(rules)}
out = []
for tf in ["H1", "M30", "M15", "M5", "M3", "M2", "M1"]:
    f = f"reports/phase4/regime_2026/s1_{tf}/split_runs.csv"
    if not os.path.exists(f): continue
    t = pd.read_csv(f); t["i"] = t.overrides.map(key)
    t["regime"] = t.i.map(lambda i: rules[i]["regime_filter"]); t["side"] = t.i.map(lambda i: rules[i]["side_filter"]); t["entry"] = t.i.map(lambda i: rules[i]["entry_mode"])
    minn = 10 if tf in ("H1", "M30") else 20
    for (sym, reg), g in t.groupby(["symbol", "regime"]):
        g = g[g.tune_trades >= minn]
        both = g[(g.tune_net_r > 0) & (g.check_net_r > 0)].sort_values("tune_net_r", ascending=False)
        best = both.iloc[0] if len(both) else (g.sort_values("tune_net_r", ascending=False).iloc[0] if len(g) else None)
        if best is None: continue
        out.append({"symbol": sym, "tf": tf, "regime": reg, "n_pass_both": len(both), "entry": best.entry, "side": best.side,
                    "jan_jul_trades": best.tune_trades, "jan_jul_r": best.tune_net_r, "jan_jul_win": best.tune_win_rate, "aug_nov_trades": best.check_trades, "aug_nov_r": best.check_net_r,
                    "bull_r": best.bull_net_r, "bear_r": best.bear_net_r, "passes": bool(best.tune_net_r > 0 and best.check_net_r > 0), "rule": int(best.i)})
r = pd.DataFrame(out); r.to_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv", index=False)
for sym, g in r.groupby("symbol"):
    print("=====", sym)
    print(g.drop(columns=["symbol"]).round(1).to_string(index=False))
