import pandas as pd, json, os
for tf in ["M3","M5","M15","M1"]:
    names=json.load(open(f"reports/phase4/rules_trval_{tf}_names.json")); rules=json.load(open(f"reports/phase4/rules_trval_{tf}.json")); key={json.dumps(r,sort_keys=True):i for i,r in enumerate(rules)}
    for per in ["2025","old"]:
        f=f"reports/phase4/trval/{tf}_{per}/split_runs.csv"
        if not os.path.exists(f): continue
        t=pd.read_csv(f); t["i"]=t.overrides.map(key)
        print(f"== {tf} {per}")
        for i,n in enumerate(names):
            g=t[t.i==i]
            if g.empty: continue
            syms=" ".join(f"{r.symbol[:4]} {r.tune_net_r:+.0f}/{r.check_net_r:+.0f}" for _,r in g.iterrows())
            print(f"  {n:62s} ALL {g.tune_trades.sum():5d} tr {g.tune_net_r.sum():+7.1f} | {g.check_trades.sum():5d} tr {g.check_net_r.sum():+7.1f} || {syms}")
