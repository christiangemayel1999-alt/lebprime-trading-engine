"""Money simulation v2 (after ChatGPT's review). Uses only trade lists with real entry and exit times:
- Gold M5 G1B and Silver M30 uptrend buys from the zone engine (exits are the engine's terminal times),
  with the 2 G1B same-candle TP1/TP2 trades corrected (tp_same_candle_affected_trades.csv);
- US30 M3 NY breakout, causal H4 trend, 15m range, TP 1.5R, 4h max hold, +0.5R lock at +1R then 1R trail (orb_v2).
Risk is sized on the CASH loss at the protective stop: zone setups use the emergency broker stop (2R from Entry 1),
the breakout uses its broker stop (1R). Lots are rounded DOWN to the broker step; a trade whose minimum lot exceeds the
budget is skipped. Risk of an open trade is released only at its real exit time."""
import math
import numpy as np, pandas as pd

B = "LebPrime AI Research/02 - Backtest Results"
VALUE = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0}
STEP = {"XAUUSD": 0.01, "XAGUSD": 0.01, "US30": 0.1}
BUCKET = {"Gold M5 G1B": "gold", "Silver M30 uptrend buys": "silver", "US30 M3 NY ORB": "us_index"}
STOP_MULT = {"Gold M5 G1B": 2.0, "Silver M30 uptrend buys": 2.0, "US30 M3 NY ORB": 1.0}


def load():
    z = pd.read_csv("reports/phase4/money_sim_trades.csv")
    z = z[z.setting.isin(["Gold M5 G1B", "Silver M30 uptrend buys"])][["setting", "symbol", "side", "entry_time", "exit_time", "risk_price", "net_r"]].copy()
    fix = pd.read_csv(f"{B}/tp_same_candle_affected_trades.csv")
    fix = fix[fix.ledger.str.startswith("G1buys")]
    for r in fix.itertuples():   # match on entry time
        m = (z.setting == "Gold M5 G1B") & (pd.to_datetime(z.entry_time, utc=True, format="mixed") == pd.Timestamp(r.entry1_time))
        z.loc[m, "net_r"] -= r.overstatement_r
    o = pd.read_csv(f"{B}/orb_v2_picks_trades.csv")
    o = o[(o.symbol == "US30") & (o.tf == "M3") & (o.tp == 1.5) & (o.hold_h == 4) & (o["mode"] == "trail")].copy()
    o["setting"] = "US30 M3 NY ORB"
    t = pd.concat([z, o[["setting", "symbol", "side", "entry_time", "exit_time", "risk_price", "net_r"]]], ignore_index=True)
    t["entry_time"] = pd.to_datetime(t.entry_time, utc=True, format="mixed"); t["exit_time"] = pd.to_datetime(t.exit_time, utc=True, format="mixed")
    return t.sort_values("entry_time").reset_index(drop=True)


def sim(t, start_eq, per_setup, bucket_cap, total_cap, daily_stop):
    eq = start_eq; peak = eq; mdd = 0.0; opens = []; day = None; day_eq = eq; taken = skipped_lot = skipped_cap = skipped_day = 0
    yearly = {}; curve = []
    for r in t.itertuples():
        opens.sort(key=lambda x: x[0])
        while opens and opens[0][0] <= r.entry_time:
            ex, pnl, cash, b = opens.pop(0); eq += pnl; yearly[ex.year] = yearly.get(ex.year, 0.0) + pnl
            peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100); curve.append((ex, eq))
        if r.entry_time.date() != day:
            day, day_eq = r.entry_time.date(), eq
        if eq <= day_eq * (1 - daily_stop / 100):
            skipped_day += 1; continue
        unit = r.risk_price * STOP_MULT[r.setting] * VALUE[r.symbol]          # cash loss per 1.0 lot at the protective stop
        lots = math.floor(eq * per_setup / 100 / unit / STEP[r.symbol] + 1e-9) * STEP[r.symbol]
        if lots < STEP[r.symbol]:
            skipped_lot += 1; continue
        cash = lots * unit; b = BUCKET[r.setting]
        if sum(c for _, _, c, bb in opens if bb == b) + cash > eq * bucket_cap / 100 or sum(c for _, _, c, _ in opens) + cash > eq * total_cap / 100:
            skipped_cap += 1; continue
        taken += 1
        opens.append((r.exit_time, r.net_r * r.risk_price * VALUE[r.symbol] * lots, cash, b))
    for ex, pnl, cash, b in sorted(opens, key=lambda x: x[0]):
        eq += pnl; yearly[ex.year] = yearly.get(ex.year, 0.0) + pnl; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
    return eq, mdd, taken, skipped_lot, skipped_cap, skipped_day, yearly


if __name__ == "__main__":
    t = load()
    t.to_csv(f"{B}/money_sim_v2_trades.csv", index=False)
    lines = [f"Trades: " + ", ".join(f"{s} {n}" for s, n in t.setting.value_counts().items())]
    for start in (1000, 10000):
        for lab, a, b_, c, d in (("ChatGPT plan: 0.25% per setup, 0.5% bucket, 1% total, 1.5% daily stop", 0.25, 0.5, 1.0, 1.5),
                                 ("Double: 0.5% / 1% / 2% / 3%", 0.5, 1.0, 2.0, 3.0),
                                 ("1% per setup / 2% bucket / 3% total / 3% daily", 1.0, 2.0, 3.0, 3.0)):
            eq, dd, tk, sl, sc, sd, yr = sim(t, start, a, b_, c, d)
            lines.append(f"${start:,} | {lab}: end ${eq:,.0f} ({(eq/start-1)*100:+.0f}%), max DD {dd:.1f}%, taken {tk}, skipped: min lot {sl}, caps {sc}, daily stop {sd} | by year " +
                         ", ".join(f"{y} ${v:+,.0f}" for y, v in sorted(yr.items())))
    open(f"{B}/money_sim_v2.txt", "w").write("\n".join(lines)); print("\n".join(lines))
