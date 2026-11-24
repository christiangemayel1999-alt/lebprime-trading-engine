"""Zone parity study, step 1: TradingView-identical zones from the bot's own engine.

Pine indicator model + the parity fixes on this branch + zone window 500 (TradingView "Recent history bars"
default: a freshly loaded chart only shows zones found in its last 500 candles). Dumps every zone that reached
LIMIT CREATED with what the trade simulation needs. No trade logic here."""
import sys, os, time
ROOT = "/var/lib/lebprime/research/zonetrail"
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd
OUT = "/var/lib/lebprime/reports/zonetrail/zones"; os.makedirs(OUT, exist_ok=True)
WIN = int(os.environ.get("WIN", "500"))
FAST = os.environ.get("FAST", "1") == "1"
START = os.environ.get("START", "2019-12-01")
END = os.environ.get("END", "")


def frame(sym, k):
    d = pd.read_csv(f"/var/lib/lebprime/mt5_data/M1/{sym}.csv")
    d = d[d.timestamp_utc >= START]
    if END: d = d[d.timestamp_utc < END]
    if k > 1:
        t = pd.to_datetime(d.timestamp_utc, utc=True)
        d = d.assign(g=(t - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(minutes=k))
        d = d.groupby("g").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                               tick_volume=("tick_volume", "sum"), spread=("spread", "last"), real_volume=("real_volume", "sum"))
        d["timestamp_utc"] = pd.to_datetime(d.index * k, unit="m", utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return d.reset_index(drop=True)[["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]]


class _NoFeatures:
    def update(self, bar): pass
    def snapshot(self): return {}


def run(sym, tf):
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    from services.indicator_lab.models import Candle
    t0 = time.time(); k = int(tf[1:]) * (60 if tf[0] == "H" else 1); fr = frame(sym, k)
    P = {**default_parameters("LEBPRIME"), "execution_model": "Pine indicator", "zone_window_bars": WIN}
    eng = LebPrimeEngine(P, symbol=sym, timeframe=tf)
    if FAST: eng._features = _NoFeatures()
    eng._last_bar_index = len(fr) - 1
    cols = list(fr.columns)
    for i, row in enumerate(fr.itertuples(index=False, name=None)):
        eng.process_bar(Candle.from_row(i, dict(zip(cols, row))))
    rows = []
    for z in eng._zones:
        if z.limit_created_time is None: continue
        rows.append({"zone_id": z.engine_zone_id, "side": z.side, "top": z.zone_top, "bottom": z.zone_bottom, "entry1": z.entry1,
                     "entry2": z.entry2, "sl": z.sl_price, "source_time": z.source_time, "candidate_time": z.candidate_time,
                     "limit_time": z.limit_created_time, "activated_time": z.entry1_activated_time, "state": z.terminal_state,
                     "end_time": z.terminal_time, "atr": z.atr_at_source, "width_atr": z.zone_width_atr, "swing_kind": z.structural_swing_kind})
    d = pd.DataFrame(rows); d["symbol"] = sym; d["tf"] = tf
    d.to_pickle(f"{OUT}/{sym}_{tf}_w{WIN}.pkl")
    print("done", sym, tf, "bars", len(fr), "zones", len(d), round(time.time() - t0), "s", flush=True)


if __name__ == "__main__":
    for job in sys.argv[1:]:
        s, t = job.split(":")
        if os.path.exists(f"{OUT}/{s}_{t}_w{WIN}.pkl") and not os.environ.get("FORCE"): print("skip", job); continue
        run(s, t)
    print("QUEUE DONE")
