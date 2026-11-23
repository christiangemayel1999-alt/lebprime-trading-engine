"""Improvement ideas applied to each portfolio setting, continuous 2020-2026, year by year."""
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from portfolio import run
VARIANTS = {"base": {}, "zone edge + 0.1 ATR, touch": {"sl_mode": "Zone edge + ATR buffer", "zone_stop_buffer_atr": 0.1, "stop_rule": "Price touch"},
            "zone edge + 0.25 ATR, touch": {"sl_mode": "Zone edge + ATR buffer", "zone_stop_buffer_atr": 0.25, "stop_rule": "Price touch"},
            "zone edge + 0.5 ATR, touch": {"sl_mode": "Zone edge + ATR buffer", "zone_stop_buffer_atr": 0.5, "stop_rule": "Price touch"},
            "ATR stop 1.0, touch": {"sl_mode": "ATR stop", "sl_atr_mult": 1.0, "stop_rule": "Price touch"}, "ATR stop 1.5, touch": {"sl_mode": "ATR stop", "sl_atr_mult": 1.5, "stop_rule": "Price touch"},
            "risk 0.5-2 ATR only": {"min_risk_atr": 0.5, "max_risk_atr": 2.0}, "TP2 at opposing zone (min 1.5R)": {"tp_mode": "Opposing zone", "tp_min_r": 1.5},
            "zone+0.25ATR touch + TP opposing zone": {"sl_mode": "Zone edge + ATR buffer", "zone_stop_buffer_atr": 0.25, "stop_rule": "Price touch", "tp_mode": "Opposing zone", "tp_min_r": 1.5}}
_OLD = {"base": {}, "emergency 1.2R": {"hard_stop_beyond_r": 0.2}, "emergency 1.5R": {"hard_stop_beyond_r": 0.5},
            "time stop 20 bars": {"time_stop_bars": 20}, "time stop 50 bars": {"time_stop_bars": 50},
            "trail 2 ATR": {"trail_atr": 2.0}, "trail 3 ATR": {"trail_atr": 3.0}, "TP1 1R": {"tp1_r": 1.0},
            "D1 trend": {"trend_filter": "With D1 trend"}, "H4 + D1 trend": {"trend_filter": "With H4 + D1 trend"}}
if __name__ == "__main__":
    rules = json.load(open("reports/phase4/regime_2026/rules.json")); best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv")
    pick = lambda s, tf, reg: rules[int(best[(best.symbol == s) & (best.tf == tf) & (best.regime == reg)].rule.iloc[0])]
    tr = json.load(open("reports/phase4/rules_trader.json")); trn = json.load(open("reports/phase4/rules_trader_names.json"))
    g1b = {**json.load(open("reports/phase4/rules_gold_final_M5.json"))[0], "side_filter": "Buys only", "limit_expiry_bars": 8640}
    g3 = json.loads(pd.read_csv("reports/phase4/persym/final_pass.csv").overrides[0])
    base = [("Gold M5 G1B", "XAUUSD", "M5", 130, g1b), ("Gold M15 G3", "XAUUSD", "M15", 124, g3),
            ("US30 M5 sweep", "US30", "M5", 132, {**tr[trn.index("Sweep reclaim | H4+base | single TP 2R | buffer 0.3 ATR")], "limit_expiry_bars": 2016}),
            ("USTEC M30 sweep sells", "USTEC", "M30", 61, pick("USTEC", "M30", "All")), ("Silver M30 uptrend buys", "XAGUSD", "M30", 59, pick("XAGUSD", "M30", "Bullish only"))]
    HB = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0, "trend_filter": "With H4 trend", "require_compact_base": True, "limit_expiry_bars": 2000}
    base += [(f"{s} M15 H4+base", s, "M15", ds, HB) for s, ds in (("XAGUSD", 125), ("US30", 126), ("USTEC", 127), ("WTI", 128), ("EURUSD", 129))]
    jobs = []
    for lab, sym, tf, ds, ov in base:
        for vn, v in VARIANTS.items():
            if vn in ("D1 trend", "H4 + D1 trend") and ov.get("regime_filter") not in (None, "All"): pass
            jobs.append((f"{lab} | {vn}", sym, tf, ds, {**ov, **v}))
    out = []
    with ProcessPoolExecutor(7) as p:
        for r in p.map(run, jobs):
            out += r
            pd.DataFrame(out).to_csv("reports/phase4/sltp_trades.csv", index=False)
    t = pd.DataFrame(out); t["year"] = t.exit_time.str[:4]
    rows = []
    for lab, g in t.groupby("setting"):
        s, v = lab.split(" | ")
        yr = g.groupby("year").net_r.sum()
        rows.append({"setting": s, "variant": v, "trades": len(g), "win": round((g.net_r > 0).mean() * 100, 1), "avg_win": round(g[g.net_r > 0].net_r.mean(), 2),
                     "avg_loss": round(g[g.net_r <= 0].net_r.mean(), 2), "net_r": round(g.net_r.sum(), 1), "losing_years": int((yr <= 0).sum()),
                     **{y: round(yr.get(y, 0.0), 1) for y in [str(x) for x in range(2020, 2027)]}})
    r = pd.DataFrame(rows); r.to_csv("LebPrime AI Research/02 - Backtest Results/sltp_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
