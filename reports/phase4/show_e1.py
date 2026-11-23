import pandas as pd, json, os, sys
names=json.load(open("reports/phase4/rules_e1_1r_names.json")); rules=json.load(open("reports/phase4/rules_e1_1r.json")); key={json.dumps(r,sort_keys=True):i for i,r in enumerate(rules)}
for tf in ["M5","M15"]:
    for per,lab in [("recent","Jun-Oct26 | Nov26"),("2025","Jan-Aug25 | Sep25-May26"),("old","2020-Jun22 | Jul22-2024")]:
        f=f"reports/phase4/e1_1r/{tf}_{per}/split_runs.csv"
        if not os.path.exists(f): continue
        t=pd.read_csv(f); t["i"]=t.overrides.map(key)
        print(f"== {tf} {lab}")
        for sym in ["ALL","XAUUSD"]:
            for i,n in enumerate(names):
                g=t[t.i==i] if sym=="ALL" else t[(t.i==i)&(t.symbol==sym)]
                if g.empty: continue
                s=f"  {sym:6s} {n:40s}"
                for p in ("tune","check"):
                    n_=g[f"{p}_trades"].sum(); nw=g[f"{p}_n_win"].sum(); nl=g[f"{p}_n_loss"].sum()
                    aw=g[f"{p}_sum_win_r"].sum()/max(nw,1); al=g[f"{p}_sum_loss_r"].sum()/max(nl,1)
                    s+=f" | {n_:5d} tr {100*nw/max(n_,1):4.1f}% w{aw:+.2f} l{al:+.2f} net {g[f'{p}_net_r'].sum():+7.1f}"
                print(s)
