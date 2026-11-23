"""Final review: for every symbol x timeframe, pick the most robust zone setting from all_results.csv and rerun it as one
continuous 2020-2026 ledger with full costs (portfolio.run), and pick the most robust NY opening-range breakout from orb_grid.csv.
Outputs per-combination metrics: trades, win %, avg win/loss, net R, year by year, buy/sell, max DD, H4 up/down split."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from portfolio import run as zone_run

B = "LebPrime AI Research/02 - Backtest Results"
SYMS = ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]
TFS = ["M1", "M2", "M3", "M5", "M10", "M15", "M30", "H1", "H4"]
BASE = {"H1": 52, "M30": 58, "M15": 124, "M5": 130, "M3": 136, "H4": 157, "M10": 163}
DS = {(s, tf): BASE[tf] + i for tf in BASE for i, s in enumerate(SYMS)}
DS.update({(s, "M2"): 142 + i for i, s in enumerate(SYMS[:4])}); DS.update({(s, "M1"): 148 + i for i, s in enumerate(SYMS[:4])})
YEARS = [str(y) for y in range(2020, 2027)]


def pick_zone():
    from services.indicator_lab.parameter_registry import validate_parameters, default_parameters
    a = pd.read_csv(f"{B}/all_results.csv", low_memory=False)
    a = a[(a.A_trades >= 15) & (a.B_trades >= 15)].copy()
    a["worst"] = a[["A_net_r", "B_net_r"]].min(axis=1); a["pos"] = (a.A_net_r > 0) & (a.B_net_r > 0)
    g = a.groupby(["symbol", "timeframe", "settings_changed_from_default"]).agg(rows=("pos", "size"), allpos=("pos", "all"), worst=("worst", "min"), total=("A_net_r", "sum")).reset_index()
    g["multi"] = g.rows >= 2
    picks = {}
    for (s, tf), x in g.groupby(["symbol", "timeframe"]):
        for _, r in x.sort_values(["allpos", "multi", "worst"], ascending=False).iterrows():
            try:
                ov = json.loads(r.settings_changed_from_default)
                if ov.get("execution_model", "Live broker") != "Live broker":
                    continue    # TradingView-style scoring (every SL = -1R) is not how a broker pays
                validate_parameters("LEBPRIME", {**default_parameters("LEBPRIME"), **ov})
            except Exception:
                continue
            picks[(s, tf)] = (ov, bool(r.allpos), int(r.rows)); break
    return picks


def metrics(t, label):
    t = t.sort_values("exit_time"); yr = t.groupby(t.exit_time.astype(str).str[:4]).net_r.sum()
    eq = t.net_r.cumsum(); w = t[t.net_r > 0]; l = t[t.net_r <= 0]
    present = [y for y in YEARS if y in yr.index]
    return {"setup": label, "trades": len(t), "win_pct": round((t.net_r > 0).mean() * 100, 1) if len(t) else 0, "avg_win": round(w.net_r.mean(), 2) if len(w) else 0,
            "avg_loss": round(l.net_r.mean(), 2) if len(l) else 0, "net_r": round(t.net_r.sum(), 1), "max_dd_r": round((eq.cummax() - eq).max(), 1) if len(t) else 0,
            "buys_r": round(t[t.side == "BUY"].net_r.sum(), 1), "sells_r": round(t[t.side == "SELL"].net_r.sum(), 1),
            "losing_years": int(sum(yr[y] <= 0 for y in present)), "years_with_data": len(present), **{y: (round(yr[y], 1) if y in yr.index else None) for y in YEARS}}


def sanity(m):
    """Flags results that are implausible for this strategy family, so they are checked before anyone sees them."""
    flags = []
    per = m["net_r"] / m["trades"] if m["trades"] else 0
    if per > 0.35: flags.append(f"net {per:+.2f}R per trade is implausibly high")
    if m["avg_win"] and m["avg_loss"] and m["win_pct"] > 55 and m["avg_win"] > abs(m["avg_loss"]) * 1.2: flags.append("high win rate AND wins much bigger than losses")
    if m["trades"] and m["max_dd_r"] < 0.01 * m["trades"] and m["net_r"] > 200: flags.append("drawdown too small for the trade count")
    return "; ".join(flags)


def h4_split(t, sym):
    from mtf import frame
    if not len(t):
        return None, None
    lf = frame(sym, "M15", "2020-01-01"); h = lf.set_index("t").close.resample("4h").last().dropna()
    up = (h.ewm(span=50, adjust=False).mean().diff() > 0); up.index = up.index + pd.Timedelta(hours=4)
    up = up[~up.index.duplicated()].rename("up").reset_index().rename(columns={"t": "time"})
    up.columns = ["time", "up"]
    x = t.assign(time=pd.to_datetime(t.entry_time, utc=True, format="mixed")).dropna(subset=["time"]).sort_values("time")
    x = pd.merge_asof(x, up.sort_values("time"), on="time")
    return round(x.net_r[x.up == True].sum(), 1), round(x.net_r[x.up == False].sum(), 1)


def job(args):
    s, tf, ov = args
    cache = f"reports/phase4/final_review/{s}_{tf}.csv"
    if os.path.exists(cache):
        t = pd.read_csv(cache)
    else:
        t = pd.DataFrame(zone_run((f"{s} {tf}", s, tf, DS[(s, tf)], ov)))
        os.makedirs("reports/phase4/final_review", exist_ok=True); t.to_csv(cache, index=False)
    if not len(t):
        return s, tf, None
    m = metrics(t, "zone")
    try:
        m["up_r"], m["down_r"] = h4_split(t, s)
    except Exception as e:
        m["up_r"], m["down_r"] = None, None; print("h4 split failed", s, tf, e, flush=True)
    return s, tf, m


if __name__ == "__main__":
    picks = pick_zone()
    jobs = [(s, tf, picks[(s, tf)][0]) for s in SYMS for tf in TFS if (s, tf) in picks and (s, tf) in DS]
    out = []
    with ProcessPoolExecutor(7) as p:
        for s, tf, m in p.map(job, sorted(jobs, key=lambda j: TFS.index(j[1]), reverse=True)):
            if m:
                ov, allpos, rows = picks[(s, tf)]
                out.append({"symbol": s, "tf": tf, "model": "Zone", "screen_all_periods_positive": allpos, "screen_rows": rows, **m, "settings": json.dumps(ov)})
            print("done", s, tf, flush=True)
    # ORB: from the grid trades (year, side, net_r per trade)
    og = pd.read_csv("reports/phase4/orb_grid_trades.csv")
    grid = pd.read_csv(f"{B}/orb_grid.csv")
    for (s, tf), x in grid.groupby(["symbol", "tf"]):
        x = x.copy(); yrs = [c for c in YEARS if c in x.columns]
        x["worst"] = x[yrs].min(axis=1, skipna=True)
        b = x.sort_values(["losing_years", "worst", "net_r"], ascending=[True, False, False]).iloc[0]
        tr = og[(og.symbol == s) & (og.tf == tf) & (og.range_min == b.range_min) & (og.tp == b.tp) & (og.hold_h == b.hold_h) &
                ((og.trend == b.trend) | (og.trend.isna() & pd.isna(b.trend)))]
        yr = tr.groupby(tr.year.astype(str)).net_r.sum(); eq = tr.net_r.cumsum()  # order within the file = chronological per config
        w = tr[tr.net_r > 0]; l = tr[tr.net_r <= 0]; present = [y for y in YEARS if y in yr.index]
        out.append({"symbol": s, "tf": tf, "model": "NY ORB", "setup": f"{int(b.range_min)}m range, TP {b.tp}R, trend {b.trend}, hold {int(b.hold_h)}h",
                    "trades": len(tr), "win_pct": round((tr.net_r > 0).mean() * 100, 1), "avg_win": round(w.net_r.mean(), 2), "avg_loss": round(l.net_r.mean(), 2),
                    "net_r": round(tr.net_r.sum(), 1), "max_dd_r": round((eq.cummax() - eq).max(), 1), "buys_r": round(tr[tr.side == "BUY"].net_r.sum(), 1),
                    "sells_r": round(tr[tr.side == "SELL"].net_r.sum(), 1), "losing_years": int(sum(yr[y] <= 0 for y in present)), "years_with_data": len(present),
                    **{y: (round(yr[y], 1) if y in yr.index else None) for y in YEARS}})
    r = pd.DataFrame(out).sort_values(["symbol", "tf", "model"])
    r["sanity_flag"] = [sanity(x) for x in r.to_dict("records")]
    if r.sanity_flag.str.len().gt(0).any():
        print("SANITY FLAGS (check before reporting):"); print(r[r.sanity_flag != ""][["symbol", "tf", "model", "net_r", "sanity_flag"]].to_string(index=False))
    r.to_csv(f"{B}/final_review_by_symbol_tf.csv", index=False)
    print(r.drop(columns=["settings"], errors="ignore").to_string(index=False)); print("ALL DONE")
