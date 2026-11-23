import pandas as pd, json, os, glob
best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv"); best = best[best.passes]
rules = json.load(open("reports/phase4/regime_2026/rules.json"))
rows = []
for _, b in best.iterrows():
    key = json.dumps(rules[b.rule], sort_keys=True); r = {"symbol": b.symbol, "tf": b.tf, "regime": b.regime, "entry": b.entry, "side": b.side, "y26a": b.jan_jul_r, "y26b": b.aug_nov_r, "n26": b.jan_jul_trades + b.aug_nov_trades}
    ok = True
    for per, cols in (("2025", ("y25a", "y25b")), ("old", ("olda", "oldb"))):
        f = f"reports/phase4/regime_val/{b.symbol}_{b.tf}_{per}/split_runs.csv"
        if not os.path.exists(f): r[cols[0]] = r[cols[1]] = None; ok = None; continue
        t = pd.read_csv(f); m = t[t.overrides == key]
        if m.empty: r[cols[0]] = r[cols[1]] = None; ok = None; continue
        r[cols[0]], r[cols[1]] = m.tune_net_r.iloc[0], m.check_net_r.iloc[0]; r[f"n{per}"] = int(m.tune_trades.iloc[0] + m.check_trades.iloc[0])
        if ok is not None: ok = ok and r[cols[0]] > 0 and r[cols[1]] > 0
    r["passes_all"] = ok; rows.append(r)
out = pd.DataFrame(rows); out.to_csv("LebPrime AI Research/02 - Backtest Results/regime_validation.csv", index=False)
print(out.round(1).to_string(index=False))
