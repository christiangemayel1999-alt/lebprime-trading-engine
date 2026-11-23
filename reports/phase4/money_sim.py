"""Money simulation of the 11-setup portfolio from $1,000: risk % of current equity, shared per symbol,
2% total open-risk cap, 3% daily loss stop, broker lot rules (Gold/Silver 0.01, US30/USTEC 0.1 min and step)."""
import sys, os, json, math
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
from portfolio import run as zone_run
from portfolio2 import orb_t

VALUE = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0, "USTEC": 1.0}
MINLOT = {"XAUUSD": 0.01, "XAGUSD": 0.01, "US30": 0.1, "USTEC": 0.1}


def job(j):
    return zone_run(j[1:]) if j[0] == "zone" else orb_t(*j[1:])


def all_jobs():
    rules = json.load(open("reports/phase4/regime_2026/rules.json")); best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_2026_best.csv")
    pick = lambda s, tf, reg: rules[int(best[(best.symbol == s) & (best.tf == tf) & (best.regime == reg)].rule.iloc[0])]
    tr = json.load(open("reports/phase4/rules_trader.json")); trn = json.load(open("reports/phase4/rules_trader_names.json"))
    g1b = {**json.load(open("reports/phase4/rules_gold_final_M5.json"))[0], "side_filter": "Buys only", "limit_expiry_bars": 8640}
    g3 = json.loads(pd.read_csv("reports/phase4/persym/final_pass.csv").overrides[0])
    hours = json.load(open("reports/phase4/own_bad_hours.json"))["XAGUSD M15 H4+base"]
    silver15 = {"tp1_execution": "Broker orders", "hard_stop_beyond_r": 1.0, "trend_filter": "With H4 trend", "require_compact_base": True, "limit_expiry_bars": 2000,
                "news_blackout_min": 60, "blocked_weekdays": "6", "blocked_hours_utc": ",".join(map(str, hours))}
    jobs = [("zone", "Gold M5 G1B", "XAUUSD", "M5", 130, g1b), ("zone", "Gold M15 G3", "XAUUSD", "M15", 124, g3),
            ("zone", "US30 M5 sweep", "US30", "M5", 132, {**tr[trn.index("Sweep reclaim | H4+base | single TP 2R | buffer 0.3 ATR")], "limit_expiry_bars": 2016}),
            ("zone", "USTEC M30 sweep sells", "USTEC", "M30", 61, pick("USTEC", "M30", "All")), ("zone", "Silver M30 uptrend buys", "XAGUSD", "M30", 59, pick("XAGUSD", "M30", "Bullish only")),
            ("zone", "Silver M15 own hours", "XAGUSD", "M15", 125, silver15),
            ("orb", "US30", "M3", 15, 1.5, "H4", 4), ("orb", "US30", "M1", 15, 2.0, "H4", 4), ("orb", "USTEC", "M1", 5, 2.0, "H4", 4),
            ("orb", "XAUUSD", "M1", 5, 2.0, "H4", 4), ("orb", "XAUUSD", "M5", 15, 2.0, "H4", 2)]
    return jobs


def collect():
    jobs = all_jobs()
    with ProcessPoolExecutor(7) as p:
        res = list(p.map(job, jobs))
    rows = []
    for j, rs in zip(jobs, res):
        for r in rs:
            r = dict(r)
            if j[0] == "zone":
                r["symbol"] = j[2]
            rows.append(r)
    t = pd.DataFrame(rows).dropna(subset=["entry_time", "risk_price"])
    t["entry_time"] = pd.to_datetime(t.entry_time, utc=True, format="mixed"); t["exit_time"] = pd.to_datetime(t.exit_time, utc=True, format="mixed")
    return t.sort_values("entry_time").reset_index(drop=True)


def simulate(t, risk_pct, allow_up_to_pct, cap_pct=2.0, daily_stop_pct=3.0):
    nset = t.groupby("symbol").setting.nunique()
    eq = 1000.0; peak = 1000.0; maxdd = 0.0; open_pos = []; day = None; day_start = eq; skipped = 0; taken = 0; by_year = {}
    events = []
    for _, r in t.iterrows():
        while open_pos and open_pos[0][0] <= r.entry_time:
            ex, pnl, rk, yr = open_pos.pop(0); eq += pnl; by_year[yr] = by_year.get(yr, 0.0) + pnl
            peak = max(peak, eq); maxdd = max(maxdd, (peak - eq) / peak * 100)
        open_pos.sort(key=lambda x: x[0])
        d = r.entry_time.date()
        if d != day:
            day, day_start = d, eq
        if eq <= day_start * (1 - daily_stop_pct / 100):
            skipped += 1; continue
        target = eq * risk_pct / 100 / nset[r.symbol]
        per_lot = r.risk_price * VALUE[r.symbol]
        lots = math.floor(target / per_lot / MINLOT[r.symbol]) * MINLOT[r.symbol]
        if lots < MINLOT[r.symbol]:
            lots = MINLOT[r.symbol]
            if lots * per_lot > eq * allow_up_to_pct / 100:
                skipped += 1; continue
        risk_usd = lots * per_lot
        if sum(p[2] for p in open_pos) + risk_usd > eq * cap_pct / 100:
            skipped += 1; continue
        taken += 1
        open_pos.append((r.exit_time, r.net_r * risk_usd, risk_usd, str(r.exit_time.year))); open_pos.sort(key=lambda x: x[0])
    for ex, pnl, rk, yr in open_pos:
        eq += pnl; by_year[yr] = by_year.get(yr, 0.0) + pnl; peak = max(peak, eq); maxdd = max(maxdd, (peak - eq) / peak * 100)
    return eq, maxdd, taken, skipped, by_year


if __name__ == "__main__":
    t = collect()
    t.to_csv("reports/phase4/money_sim_trades.csv", index=False)
    lines = [f"{len(t)} trades available"]
    for rp, up in ((0.5, 0.5), (0.5, 1.0), (1.0, 1.0), (1.0, 2.0)):
        eq, dd, tk, sk, yrs = simulate(t, rp, up)
        lines.append(f"risk {rp}% of equity (min-lot trades allowed up to {up}%): end ${eq:,.0f}, max DD {dd:.1f}%, trades taken {tk}, skipped {sk}; profit by year " +
                     ", ".join(f"{y}: ${v:+,.0f}" for y, v in sorted(yrs.items())))
    open("reports/phase4/money_sim.txt", "w").write("\n".join(lines)); print("\n".join(lines)); print("ALL DONE")
