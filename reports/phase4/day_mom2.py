import sys, os, math
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
import pandas as pd, MetaTrader5 as mt5
import day_test_default as d
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters
DAY = "2026-11-23"; mt5.initialize()
base = {**default_parameters("LEBPRIME"), "gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0, "stop_rule": "Price touch"}
ALL = ["M1", "M3", "M5", "M15", "M30", "M45"]; HI = ["M5", "M15", "M30", "M45"]
FILT = {"entry2_mode": "Off", "max_spread_risk": 0.1, "block_beirut_window": "15:00-17:00"}
V = {"momentum 4 (original)": ({}, ALL), "momentum 2": ({"momentum_count": 2}, ALL), "momentum 4 + filters": (FILT, HI), "momentum 2 + filters": ({"momentum_count": 2, **FILT}, HI)}
info = {s: mt5.symbol_info(s) for s in d.SYMS}
frames = {(s, tf): d.frame(s, tf, 5000) for s in d.SYMS for tf in ALL}
mt5.shutdown()
rows = []
for lab, (ov, tfs) in V.items():
    for s in d.SYMS:
        i = info[s]; value = i.trade_tick_value / i.trade_tick_size
        for tf in tfs:
            for z in LebPrimeEngine({**base, **ov}, symbol=s, timeframe=tf).run(frames[(s, tf)])["zones"]:
                if z.entry1_activated_time is None or z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}: continue
                et = pd.Timestamp(z.entry1_activated_time); et = et.tz_localize("UTC") if et.tz is None else et
                xt = pd.Timestamp(z.terminal_time); xt = xt.tz_localize("UTC") if xt.tz is None else xt
                if DAY not in (et.tz_convert("Asia/Beirut").strftime("%Y-%m-%d"), xt.tz_convert("Asia/Beirut").strftime("%Y-%m-%d")): continue
                risk = abs(float(z.entry_price) - float(z.sl_price)); legs = sum(1 for l in z.metadata["live_legs"].values() if l.get("filled")) or 1
                single = ov.get("entry2_mode") == "Off"
                lot = math.floor((20.0 if single else 10.0) / (risk * value) / i.volume_step + 1e-9) * i.volume_step
                cost_r = (float(z.metadata.get("entry1_spread") or 0) * i.point + d.COMM[s] / value) / risk * (1.0 if single else 0.5 * legs)
                r = float(z.result_r or 0) - cost_r
                rows.append({"variant": lab, "symbol": s, "tf": tf, "side": "BUY" if z.side == "DEMAND" else "SELL", "r": r,
                             "usd": r * 20.0 if lot >= i.volume_min else None})
t = pd.DataFrame(rows); t.to_csv(f"LebPrime AI Research/02 - Backtest Results/day_mom2_{DAY}.csv", index=False)
for lab, g in t.groupby("variant", sort=False):
    w = g[g.r > 0]; l = g[g.r <= 0]
    print(f"{lab}: {len(g)} trades, win {len(w)/len(g)*100:.0f}%, avg +{w.r.mean():.2f}/{l.r.mean():.2f}, net {g.r.sum():+.1f}R, ${g.usd.sum():+.0f} | buys {g[g.side=='BUY'].r.sum():+.1f} sells {g[g.side=='SELL'].r.sum():+.1f} | " +
          ", ".join(f"{k} {v:+.1f}" for k, v in g.groupby("symbol").r.sum().items()))
