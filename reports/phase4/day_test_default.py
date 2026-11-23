"""One-day test of the ORIGINAL LebPrime settings (registry defaults + live config buffers), with a fixed broker SL
(stop_rule 'Price touch') instead of the candle-close SL. All 6 symbols, the live bot's timeframes M1 M3 M5 M15 M30 M45
(M45 built from M15). $1,000 account, 2% risk per zone (two entries at 1% each, equal lots as the live bot)."""
import sys, os, math, time
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
import pandas as pd
import MetaTrader5 as mt5
import phase4_search
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-11-23"
BEIRUT = "Asia/Beirut"
SYMS = ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]
TF = {"M1": mt5.TIMEFRAME_M1, "M3": mt5.TIMEFRAME_M3, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30}
COMM = {"XAUUSD": 7.0, "XAGUSD": 7.0, "WTI": 7.0, "EURUSD": 7.0, "US30": 0.7, "USTEC": 0.7}


def frame(sym, tf, bars):
    src = "M15" if tf == "M45" else tf
    r = pd.DataFrame(mt5.copy_rates_from_pos(sym, TF[src], 0, bars * (3 if tf == "M45" else 1)))
    off = round((mt5.symbol_info_tick(sym).time - time.time()) / 3600)
    r["t"] = pd.to_datetime(r.time, unit="s", utc=True) - pd.Timedelta(hours=off)
    if tf == "M45":
        r = r.set_index("t").resample("45min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "max", "real_volume": "sum"}).dropna().reset_index()
    return pd.DataFrame({"timestamp_utc": r.t.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                         "tick_volume": r.tick_volume.astype(float), "spread": r.spread.astype(float), "real_volume": r.real_volume.astype(float)})


if __name__ == "__main__":
    mt5.initialize()
    params = {**default_parameters("LEBPRIME"), "gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0, "stop_rule": "Price touch"}
    rows = []; cover = {}
    for sym in SYMS:
        info = mt5.symbol_info(sym)
        if info is None:
            continue
        value = info.trade_tick_value / info.trade_tick_size; step = info.volume_step; vmin = info.volume_min
        for tf in ["M1", "M3", "M5", "M15", "M30", "M45"]:
            f = frame(sym, tf, 5000); cover[sym] = f.timestamp_utc.iloc[-1]
            res = LebPrimeEngine(params, symbol=sym, timeframe=tf).run(f)
            for z in res["zones"]:
                if z.entry1_activated_time is None:
                    continue
                et = pd.Timestamp(z.entry1_activated_time); et = et.tz_localize("UTC") if et.tz is None else et
                xt = pd.Timestamp(z.terminal_time) if z.terminal_time is not None else None
                if xt is not None and xt.tz is None:
                    xt = xt.tz_localize("UTC")
                day_e = et.tz_convert(BEIRUT).strftime("%Y-%m-%d"); day_x = xt.tz_convert(BEIRUT).strftime("%Y-%m-%d") if xt is not None else None
                closed = z.terminal_state in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}
                if not (day_e == DAY or day_x == DAY or (not closed and day_e <= DAY)):
                    continue
                risk = abs(float(z.entry_price) - float(z.sl_price))
                legs = sum(1 for l in (z.metadata.get("live_legs") or {}).values() if l.get("filled")) or 1
                lot = math.floor(10.0 / (risk * value) / step + 1e-9) * step   # 1% of $1,000 per entry, equal lots
                lot_note = "" if lot >= vmin else f"below broker minimum {vmin} (would be skipped)"
                lot = max(lot, 0)
                spread_cost = float(z.metadata.get("entry1_spread") or 0) * info.point * value * lot * legs
                comm = COMM[sym] * lot * legs
                gross_usd = float(z.result_r or 0) * risk * value * (2 * lot) if closed else None   # result_r is on the whole position (2 entries = 2x lot of risk)
                rows.append({"symbol": sym, "tf": tf, "side": "BUY" if z.side == "DEMAND" else "SELL",
                             "entry1_beirut": et.tz_convert(BEIRUT).strftime("%d %b %H:%M"), "exit_beirut": xt.tz_convert(BEIRUT).strftime("%d %b %H:%M") if xt is not None else "open",
                             "entry1": round(float(z.entry1 or z.entry_price), 3), "entry2": round(float(z.entry2), 3) if getattr(z, "entry2", None) else None,
                             "sl": round(float(z.sl_price), 3), "tp1": round(float(z.tp1_price or 0), 3), "tp2": round(float(z.tp2_price or 0), 3),
                             "legs_filled": legs, "result": z.terminal_state or "OPEN", "net_r": round(float(z.result_r or 0) - ((spread_cost + comm) / (risk * value * 2 * lot) if lot else 0), 2) if closed else None,
                             "lot_per_entry": round(lot, 2), "usd": round(gross_usd - spread_cost - comm, 2) if closed and lot else None, "note": lot_note})
    mt5.shutdown()
    t = pd.DataFrame(rows)
    t.to_csv(f"LebPrime AI Research/02 - Backtest Results/day_test_original_fixedSL_{DAY}.csv", index=False)
    print("Data up to (UTC):", cover)
    if len(t):
        print(t.to_string(index=False))
        print(t.groupby("symbol").agg(trades=("result", "size"), closed=("net_r", "count"), wins=("net_r", lambda x: int((x > 0).sum())), net_r=("net_r", "sum"), usd=("usd", "sum")).to_string())
    print("ALL DONE")
