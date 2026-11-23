"""Higher-risk variants of the $1,000 money simulation, plus losing streak and Monte Carlo trade-order shuffle."""
import sys, os, math
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "reports/phase4")
import numpy as np, pandas as pd
from money_sim import VALUE, MINLOT

t = pd.read_csv("reports/phase4/money_sim_trades.csv")
t["entry_time"] = pd.to_datetime(t.entry_time, utc=True, format="mixed"); t["exit_time"] = pd.to_datetime(t.exit_time, utc=True, format="mixed")
t = t.sort_values("entry_time").reset_index(drop=True)
nset = t.groupby("symbol").setting.nunique()


def sim(risk_pct, cap_pct, daily_pct):
    eq = 1000.0; open_pos = []; day = None; day_start = eq; closed = []  # (exit_time, pnl, equity_before_close)
    for r in t.itertuples():
        while open_pos and open_pos[0][0] <= r.entry_time:
            ex, pnl, rk = open_pos.pop(0); closed.append((ex, pnl, eq)); eq += pnl
        if r.entry_time.date() != day:
            day, day_start = r.entry_time.date(), eq
        if eq <= day_start * (1 - daily_pct / 100) or eq <= 0:
            continue
        per_lot = r.risk_price * VALUE[r.symbol]; target = eq * risk_pct / 100 / nset[r.symbol]
        lots = max(MINLOT[r.symbol], math.floor(target / per_lot / MINLOT[r.symbol]) * MINLOT[r.symbol])
        risk_usd = lots * per_lot
        if risk_usd > eq * risk_pct / 100 or sum(p[2] for p in open_pos) + risk_usd > eq * cap_pct / 100:
            continue
        open_pos.append((r.exit_time, r.net_r * risk_usd, risk_usd)); open_pos.sort(key=lambda x: x[0])
    for ex, pnl, rk in open_pos:
        closed.append((ex, pnl, eq)); eq += pnl
    c = pd.DataFrame(closed, columns=["exit", "pnl", "eq_before"])
    curve = 1000 + c.pnl.cumsum(); dd = ((curve.cummax() - curve) / curve.cummax()).max() * 100
    years = c.groupby(c.exit.dt.year).pnl.sum().cumsum() + 1000
    # longest losing streak and its drop
    best = (0, 0.0); n = 0; start = None
    for i, p in enumerate(c.pnl):
        if p <= 0:
            if n == 0:
                start = 1000 + (c.pnl.iloc[:i].sum())
            n += 1; cur = 1000 + c.pnl.iloc[:i + 1].sum()
            if n > best[0]:
                best = (n, (start - cur) / start * 100)
        else:
            n = 0
    frac = (c.pnl / c.eq_before).to_numpy()
    rng = np.random.default_rng(7); dds = []; ruin = 0
    for _ in range(2000):
        e = np.cumprod(1 + rng.permutation(frac)); d = ((np.maximum.accumulate(e) - e) / np.maximum.accumulate(e)).max() * 100
        dds.append(d); ruin += d >= 50
    return eq, dd, years, best, np.percentile(dds, 95), ruin / 20, len(c)


lines = []
for rp, cap, dl in ((1, 2, 3), (2, 2, 3), (2, 4, 6), (3, 3, 3), (3, 6, 9)):
    eq, dd, yrs, (sn, sd), p95, ruin, n = sim(rp, cap, dl)
    lines.append(f"risk {rp}% cap {cap}% daily stop {dl}%: end ${eq:,.0f}, max DD {dd:.1f}%, trades {n}, longest losing streak {sn} trades (-{sd:.1f}%), "
                 f"Monte Carlo worst-5% DD {p95:.1f}%, chance of a 50% DD {ruin:.1f}%; year-end " + ", ".join(f"{y}: ${v:,.0f}" for y, v in yrs.items()))
open("reports/phase4/money_sim_risk.txt", "w").write("\n".join(lines)); print("\n".join(lines))
