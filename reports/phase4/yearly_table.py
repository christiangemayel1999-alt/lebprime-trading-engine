import pandas as pd, sys
t = pd.read_csv("reports/phase4/yearly_trades.csv")
order = ["M1", "M2", "M3", "M5", "M15", "M30", "H1"]
years = [str(y) for y in range(2020, 2027)]
for sym in (sys.argv[1:] or sorted(t.symbol.unique())):
    g = t[t.symbol == sym]
    print(f"\n**{sym}** (uptrend + downtrend settings combined; win %, net R)\n")
    print("| TF | " + " | ".join(years) + " | Total |"); print("|" + "---|" * (len(years) + 2))
    for tf in [o for o in order if o in g.tf.unique()]:
        x = g[g.tf == tf]; cells = []
        for y in years:
            yy = x[x.year.astype(str) == y]
            cells.append(f"{(yy.net_r > 0).mean()*100:.0f}% {yy.net_r.sum():+.0f}" if len(yy) else "–")
        cells.append(f"**{(x.net_r > 0).mean()*100:.0f}% {x.net_r.sum():+.0f}R**")
        print(f"| {tf} | " + " | ".join(cells) + " |")
