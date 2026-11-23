"""Combined portfolio: zone settings + NY opening-range breakout picks, year by year, full risk vs shared risk per symbol.
Also a London-open breakout check for Gold/US30/USTEC."""
import sys, os, itertools
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from mtf import frame, cost_r, manage, START, MINUTES


def orb_t(sym, ltf, mins, tp, trend, hold_h, city="NY"):
    from zoneinfo import ZoneInfo
    lf = frame(sym, ltf, START.get(ltf, "2020-01-01"))
    h = lf.set_index("t").close.resample({"H1": "1h", "H4": "4h"}[trend]).last().dropna()
    up = (h.ewm(span=50, adjust=False).mean().diff() > 0)
    lo, hi, cl, sp = (lf[c].to_numpy(float) for c in ("low", "high", "close", "spread"))
    step = MINUTES[ltf]
    tz, hh, mm = {"NY": ("America/New_York", 9, 30), "London": ("Europe/London", 8, 0)}[city]
    loc = lf.t.dt.tz_convert(ZoneInfo(tz))
    rows = []
    for i0 in np.flatnonzero(((loc.dt.hour == hh) & (loc.dt.minute == mm)).to_numpy()):
        n = max(1, mins // step)
        if i0 + n + 1 >= len(cl):
            continue
        rh, rl = hi[i0:i0 + n].max(), lo[i0:i0 + n].min()
        if rh <= rl:
            continue
        prior = up[up.index < lf.t[i0]]
        tu = bool(prior.iloc[-1]) if len(prior) else None
        for i in range(i0 + n, min(len(cl), i0 + n + 120 // step)):
            buy, sell = cl[i] > rh, cl[i] < rl
            if not (buy or sell):
                continue
            if (buy and tu is False) or (sell and tu is True):
                break
            entry = cl[i]; stop = rl if buy else rh; risk = abs(entry - stop)
            tgt = entry + tp * risk if buy else entry - tp * risk
            r = manage(lo, hi, cl, i, entry, stop, tgt, buy, hold_h * 60 // step)
            rows.append({"setting": f"{sym} {ltf} {city} breakout {mins}m TP{tp} {trend}", "symbol": sym, "side": "BUY" if buy else "SELL",
                         "exit_time": str(lf.t[min(len(cl) - 1, i + 1)]), "entry_time": str(lf.t[i]), "risk_price": risk, "net_r": r - cost_r(sym, sp[i], risk)})
            break
    return rows


def run(job):
    return orb_t(*job)


if __name__ == "__main__":
    picks = [("US30", "M3", 15, 1.5, "H4", 4), ("US30", "M1", 15, 2.0, "H4", 4), ("USTEC", "M1", 5, 2.0, "H4", 4),
             ("XAUUSD", "M1", 5, 2.0, "H4", 4), ("XAUUSD", "M5", 15, 2.0, "H4", 2)]
    london = [(s, tf, m, tp, "H4", 4, "London") for s in ("XAUUSD", "US30", "USTEC") for tf in ("M1", "M3", "M5") for m in (15, 30) for tp in (1.5, 2.0)]
    with ProcessPoolExecutor(7) as p:
        orb_rows = list(p.map(run, picks))
        lon_rows = list(p.map(run, london))
    B = "LebPrime AI Research/02 - Backtest Results"
    zone = pd.read_csv(f"{B}/portfolio_trades_2020_2026.csv")
    zone["symbol"] = zone.setting.map(lambda s: {"Gold": "XAUUSD", "US30": "US30", "USTEC": "USTEC", "Silver": "XAGUSD"}[s.split()[0]])
    hm = pd.read_csv("reports/phase4/hourmap_trades.csv")
    silver = hm[hm.setting == "XAGUSD M15 H4+base | own losing hours + news60 + Sunday"].copy()
    silver["setting"] = "Silver M15 H4+base, own hours"; silver["symbol"] = "XAGUSD"
    orb = pd.DataFrame([r for rows in orb_rows for r in rows])
    allt = pd.concat([zone[["setting", "symbol", "side", "exit_time", "net_r"]], silver[["setting", "symbol", "side", "exit_time", "net_r"]], orb], ignore_index=True)
    allt["year"] = allt.exit_time.str[:4]
    allt = allt.sort_values("exit_time")
    allt.to_csv(f"{B}/portfolio2_trades_2020_2026.csv", index=False)
    nset = allt.groupby("symbol").setting.nunique()
    allt["w_shared"] = allt.symbol.map(lambda s: 1.0 / nset[s])

    def stat(g, col):
        x = g.net_r * g[col] if col else g.net_r
        eq = x.cumsum()
        return f"{x.sum():+.1f}R (DD {(eq.cummax() - eq).max():.1f})"

    lines = ["| Part | " + " | ".join(str(y) for y in range(2020, 2027)) + " | Total |", "|" + "---|" * 9]
    for s, g in allt.groupby("setting"):
        yr = g.groupby("year").net_r.sum()
        lines.append(f"| {s} | " + " | ".join(f"{yr.get(str(y), 0):+.1f}" for y in range(2020, 2027)) + f" | {g.net_r.sum():+.1f} ({len(g)} tr, {(g.net_r > 0).mean()*100:.0f}%) |")
    for lab, col in (("PORTFOLIO full risk each", None), ("PORTFOLIO risk shared per symbol", "w_shared")):
        cells = [stat(allt[allt.year == str(y)], col) for y in range(2020, 2027)]
        lines.append(f"| **{lab}** | " + " | ".join(cells) + f" | **{stat(allt, col)}** |")
    months = (pd.to_datetime(allt.exit_time.max(), utc=True) - pd.to_datetime(allt.exit_time.min(), utc=True)).days / 30.4
    lines.append(f"trades {len(allt)}, per month {len(allt)/months:.1f}, win {(allt.net_r>0).mean()*100:.1f}%, avg win {allt[allt.net_r>0].net_r.mean():.2f}, avg loss {allt[allt.net_r<=0].net_r.mean():.2f}")
    lon = pd.DataFrame([r for rows in lon_rows for r in rows])
    lon["year"] = lon.exit_time.str[:4]
    for s, g in lon.groupby("setting"):
        yr = g.groupby("year").net_r.sum()
        lines.append(f"LONDON {s}: {g.net_r.sum():+.1f}R, {len(g)} tr, losing years {int((yr <= 0).sum())}")
    open("reports/phase4/portfolio2.txt", "w").write("\n".join(lines))
    print("\n".join(lines)); print("ALL DONE")
