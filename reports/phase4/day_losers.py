import sys, os, math
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
import pandas as pd, MetaTrader5 as mt5
import day_test_default as d
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters
DAY = "2026-11-23"
mt5.initialize()
P = {**default_parameters("LEBPRIME"), "gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0, "stop_rule": "Price touch"}
rows = []
for sym in d.SYMS:
    info = mt5.symbol_info(sym); value = info.trade_tick_value / info.trade_tick_size
    for tf in ["M1", "M3", "M5", "M15", "M30", "M45"]:
        f = d.frame(sym, tf, 5000)
        for z in LebPrimeEngine(P, symbol=sym, timeframe=tf).run(f)["zones"]:
            if z.entry1_activated_time is None or z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}: continue
            et = pd.Timestamp(z.entry1_activated_time); et = et.tz_localize("UTC") if et.tz is None else et
            xt = pd.Timestamp(z.terminal_time); xt = xt.tz_localize("UTC") if xt.tz is None else xt
            if et.tz_convert("Asia/Beirut").strftime("%Y-%m-%d") != DAY and xt.tz_convert("Asia/Beirut").strftime("%Y-%m-%d") != DAY: continue
            m = z.metadata; risk = abs(float(z.entry_price) - float(z.sl_price)); legs = sum(1 for l in m["live_legs"].values() if l.get("filled"))
            cost_price = float(m.get("entry1_spread") or 0) * info.point + d.COMM[sym] / value
            buy = z.side == "DEMAND"; h4 = m.get("entry1_h4_trend"); h1 = m.get("entry1_h1_trend")
            rows.append({"symbol": sym, "tf": tf, "side": "BUY" if buy else "SELL", "hour_beirut": et.tz_convert("Asia/Beirut").hour, "session": m.get("entry1_session"),
                         "h4": "with" if (h4 == "UP") == buy and h4 in ("UP", "DOWN") else ("against" if h4 in ("UP", "DOWN") else "flat"),
                         "h1": "with" if (h1 == "UP") == buy and h1 in ("UP", "DOWN") else ("against" if h1 in ("UP", "DOWN") else "flat"),
                         "age_bars": z.zone_age_at_activation, "retests": z.retest_count, "legs": legs, "stop_atr": risk / float(m.get("entry1_atr14") or z.atr_at_source or 1),
                         "cost_share": cost_price / risk if risk else 0, "mfe_r": float(z.mfe_r or 0), "tp1_r": float(m.get("tp1_r_used") or 1), "rsi": m.get("entry1_rsi14"),
                         "base": m.get("base_detection_mode"), "result": z.terminal_state, "r": float(z.result_r or 0) - cost_price / risk * (0.5 * legs)})
mt5.shutdown()
t = pd.DataFrame(rows); t.to_csv(f"LebPrime AI Research/02 - Backtest Results/day_trades_features_{DAY}.csv", index=False)
print(len(t), "closed trades; losers", int((t.r <= 0).sum()), "net", round(t.r.sum(), 1))
for col in ["tf", "h4", "h1", "side", "legs", "session", "base"]:
    print(t.groupby(col).r.agg(trades="size", net="sum", win=lambda x: round((x > 0).mean() * 100)).round(1).to_string(), "\n")
t["stop_bin"] = pd.cut(t.stop_atr, [0, 0.5, 1, 2, 99]); t["cost_bin"] = pd.cut(t.cost_share, [0, 0.05, 0.1, 0.2, 9]); t["age_bin"] = pd.cut(t.age_bars, [-1, 20, 100, 500, 1e9])
for col in ["stop_bin", "cost_bin", "age_bin"]:
    print(t.groupby(col, observed=True).r.agg(trades="size", net="sum", win=lambda x: round((x > 0).mean() * 100)).round(1).to_string(), "\n")
L = t[t.r <= 0]
print("losers MFE: reached >= half of TP1:", int((L.mfe_r >= 0.5 * L.tp1_r).sum()), "of", len(L), "| median MFE R", round(L.mfe_r.median(), 2))
