"""Winners vs losers profile for a trade table. Usage: winloss.py <csv> [filter_col=value]"""
import sys
import pandas as pd, numpy as np
def profile(t, label):
    t = t.copy(); t["win"] = t.net_r > 0
    buy = t.side == "BUY"
    for pre in ("limit", "entry1"):
        if f"{pre}_rsi14" in t: t[f"{pre}_rsi_trade_side"] = np.where(buy, t[f"{pre}_rsi14"], 100 - t[f"{pre}_rsi14"])
        if f"{pre}_ema200_dist_atr" in t: t[f"{pre}_ema200_trade_side"] = np.where(buy, t[f"{pre}_ema200_dist_atr"], -t[f"{pre}_ema200_dist_atr"])
        if f"{pre}_ema50_slope_atr" in t: t[f"{pre}_slope_trade_side"] = np.where(buy, t[f"{pre}_ema50_slope_atr"], -t[f"{pre}_ema50_slope_atr"])
        if f"{pre}_macd_hist" in t: t[f"{pre}_macd_trade_side"] = np.where(buy, t[f"{pre}_macd_hist"], -t[f"{pre}_macd_hist"])
    out = [f"### {label}", f"{len(t)} trades, {t.win.mean()*100:.1f}% winners, net {t.net_r.sum():+.1f}R, avg win {t[t.win].net_r.mean():+.2f}R, avg loss {t[~t.win].net_r.mean():+.2f}R"]
    if "state" in t:
        s = t.groupby("state").net_r.agg(["size", "sum", "mean"]).round(2); out.append("Exit types: " + "; ".join(f"{i} {int(r['size'])} tr, {r['sum']:+.0f}R ({r['mean']:+.2f})" for i, r in s.iterrows()))
    for col in ["side", "legs", "entries_filled", "limit_session", "entry1_session", "limit_h4_trend", "entry1_h4_trend"]:
        if col in t and t[col].notna().any():
            g = t.groupby(col).agg(n=("net_r", "size"), win=("win", "mean"), avg=("net_r", "mean"))
            out.append(f"{col}: " + "; ".join(f"{i}: {int(r.n)} tr, {r.win*100:.0f}% win, {r.avg:+.3f}R" for i, r in g.iterrows()))
    if "side" in t and "limit_h4_trend" in t:
        al = ((t.side == "BUY") & (t.limit_h4_trend == "UP")) | ((t.side == "SELL") & (t.limit_h4_trend == "DOWN"))
        out.append(f"with H4 trend: {al.mean()*100:.0f}% of trades, avg {t[al].net_r.mean():+.3f}R vs against {t[~al].net_r.mean():+.3f}R")
    num = [c for c in ["fill_delay_bars", "zone_width_atr", "risk_atr", "departure_atr", "limit_rsi_trade_side", "entry1_rsi_trade_side", "limit_adx14", "entry1_adx14", "limit_atr14_percentile", "entry1_atr14_percentile",
                       "limit_ema200_trade_side", "limit_slope_trade_side", "entry1_macd_trade_side", "limit_bb_width_atr", "limit_tick_volume_ratio", "mfe_r", "mae_r"] if c in t and t[c].notna().sum() > 50]
    out.append("| Value | Winners median | Losers median | Best quintile (avg R) | Worst quintile (avg R) |"); out.append("| --- | --- | --- | --- | --- |")
    for c in num:
        x = t[[c, "net_r", "win"]].dropna()
        try:
            q = x.groupby(pd.qcut(x[c], 5, duplicates="drop"), observed=True).net_r.mean()
        except ValueError:
            continue
        out.append(f"| {c} | {x[x.win][c].median():.2f} | {x[~x.win][c].median():.2f} | {q.idxmax()} ({q.max():+.3f}) | {q.idxmin()} ({q.min():+.3f}) |")
    return "\n".join(out)
if __name__ == "__main__":
    t = pd.read_csv(sys.argv[1], low_memory=False)
    print(profile(t, sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]))
