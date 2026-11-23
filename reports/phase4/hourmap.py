"""Per-setting losing-hour map: pick hours from the setting's own 2020-2023 trades, test on 2024-2026 (plus news 60 + no Sunday)."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from portfolio import run
if __name__ == "__main__":
    rules = json.load(open("reports/phase4/regime_2026/rules.json")); best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv")
    pick = lambda s, tf, reg: rules[int(best[(best.symbol == s) & (best.tf == tf) & (best.regime == reg)].rule.iloc[0])]
    tr = json.load(open("reports/phase4/rules_trader.json")); trn = json.load(open("reports/phase4/rules_trader_names.json"))
    g1b = {**json.load(open("reports/phase4/rules_gold_final_M5.json"))[0], "side_filter": "Buys only", "limit_expiry_bars": 8640}
    g3 = json.loads(pd.read_csv("reports/phase4/persym/final_pass.csv").overrides[0])
    HB = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0, "trend_filter": "With H4 trend", "require_compact_base": True, "limit_expiry_bars": 2000}
    base = [("Gold M5 G1B", "XAUUSD", "M5", 130, g1b), ("Gold M15 G3", "XAUUSD", "M15", 124, g3),
            ("US30 M5 sweep", "US30", "M5", 132, {**tr[trn.index("Sweep reclaim | H4+base | single TP 2R | buffer 0.3 ATR")], "limit_expiry_bars": 2016}),
            ("USTEC M30 sweep sells", "USTEC", "M30", 61, pick("USTEC", "M30", "All")), ("Silver M30 uptrend buys", "XAGUSD", "M30", 59, pick("XAGUSD", "M30", "Bullish only"))]
    base += [(f"{s} M15 H4+base", s, "M15", ds, HB) for s, ds in (("XAGUSD", 125), ("US30", 126), ("USTEC", 127), ("WTI", 128), ("EURUSD", 129))]
    with ProcessPoolExecutor(7) as p:
        first = list(p.map(run, base))
    jobs = []; maps = {}
    for (lab, sym, tf, ds, ov), rows in zip(base, first):
        t = pd.DataFrame(rows); t = t[t.limit_time < "2024-01-01"]
        t["hour"] = pd.to_datetime(t.limit_time, utc=True).dt.hour
        h = t.groupby("hour").net_r.agg(["size", "mean"])
        bad = h[(h["size"] >= 8) & (h["mean"] < -0.15)].index.tolist()
        maps[lab] = bad
        extra = {"news_blackout_min": 60, "blocked_weekdays": "6"}
        if bad: jobs.append((f"{lab} | own losing hours + news60 + Sunday", sym, tf, ds, {**ov, **extra, "blocked_hours_utc": ",".join(map(str, bad))}))
        jobs.append((f"{lab} | news60 + Sunday", sym, tf, ds, {**ov, **extra}))
    json.dump(maps, open("reports/phase4/own_bad_hours.json", "w"))
    with ProcessPoolExecutor(7) as p:
        second = list(p.map(run, jobs))
    allrows = [r for rows, b in zip(first, base) for r in [{**x, "setting": f"{b[0]} | base"} for x in rows]] + [r for rows in second for r in rows]
    t = pd.DataFrame(allrows); t["year"] = t.exit_time.str[:4]; t.to_csv("reports/phase4/hourmap_trades.csv", index=False)
    out = []
    for lab, g in t.groupby("setting"):
        s, v = lab.split(" | "); yr = g.groupby("year").net_r.sum()
        out.append({"setting": s, "variant": v, "hours_dropped": ",".join(map(str, maps.get(s, []))) if "own" in v else "", "trades": len(g), "win": round((g.net_r > 0).mean()*100, 1),
                    "avg_win": round(g[g.net_r > 0].net_r.mean(), 2), "avg_loss": round(g[g.net_r <= 0].net_r.mean(), 2), "net_r": round(g.net_r.sum(), 1),
                    "r_2020_23": round(g[g.year < "2024"].net_r.sum(), 1), "r_2024_26": round(g[g.year >= "2024"].net_r.sum(), 1), "losing_years": int((yr <= 0).sum()),
                    **{y: round(yr.get(y, 0.0), 1) for y in [str(x) for x in range(2020, 2027)]}})
    r = pd.DataFrame(out); r.to_csv("LebPrime AI Research/02 - Backtest Results/hourmap_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
