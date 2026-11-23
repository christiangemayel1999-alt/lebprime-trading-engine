"""VSS3-style zones (code in LebPrime/VSS3 Live.txt) with the exits seen on Christian's newer indicator:
SL = far edge -/+ $2, TP1 = Entry 1 +/- $3 (close half, stop to entry), TP = Entry 1 +/- $7 (close rest, or trail after TP).
Optional F flip: an original zone whose trade is stopped before TP1 becomes the opposite zone at the same bounds.
Gold, M1/M2/M3/M5, 2020-2026, spread + $7/lot commission. Results in R (risk = Entry 1 to SL)."""
import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from mtf import frame, START

POINT, COMM = 0.01, 0.07   # Gold: spread points -> price; $7 per lot round trip = 0.07 price per oz-lot


def run(args):
    tf, flip, exit_mode = args
    f = frame("XAUUSD", tf, START.get(tf, "2020-01-01"))
    t = f.t.to_numpy(); o, h, l, c, sp = (f[k].to_numpy(float) for k in ("open", "high", "low", "close", "spread"))
    n = len(c); body = np.abs(c - o)
    avg = pd.Series(body).rolling(20).mean().to_numpy()
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.r_[c[0], c[:-1]]), np.abs(l - np.r_[c[0], c[:-1]])))
    atr = pd.Series(tr).ewm(alpha=1 / 20, adjust=False).mean().to_numpy()
    zones = []      # dict: side, top, bot, born, flipped, state ('wait','live','done')
    trades = []     # open trades
    rows = []
    last = {"BUY": -99, "SELL": -99}
    for i in range(30, n):
        # 1) new zones (4 of last 4 candles, body >= 0.5 x SMA20 body; source = candle i-5; clamp 1.5 ATR)
        for side in ("BUY", "SELL"):
            cnt = 0
            for j in range(i - 3, i + 1):
                if body[j] >= avg[i] * 0.5 and ((c[j] > o[j]) if side == "BUY" else (c[j] < o[j])):
                    cnt += 1
            if cnt >= 4 and i - last[side] > 5:
                last[side] = i
                s = i - 5; top, bot = h[s], l[s]; w = top - bot; mx = 1.5 * atr[i]
                if w > mx:
                    top -= (w - mx) / 2; bot += (w - mx) / 2
                if not any(z["side"] == side and z["state"] != "done" and z["bot"] <= top and bot <= z["top"] for z in zones):
                    zones.append({"side": side, "top": top, "bot": bot, "born": s, "flipped": False, "state": "wait"})
        # 3) zone activation / invalidation
        for z in zones:
            if z["state"] != "wait" or i - z["born"] < 10:
                continue
            buy = z["side"] == "BUY"
            entry = z["top"] if buy else z["bot"]
            touched = (l[i] <= entry) if buy else (h[i] >= entry)
            if not touched and ((min(o[i], c[i]) < z["bot"]) if buy else (max(o[i], c[i]) > z["top"])):
                z["state"] = "done"; continue
            if touched:
                if (buy and o[i] < entry) or (not buy and o[i] > entry):
                    continue    # bar opened on the wrong side; LIMIT would not fill as a retest
                stop = z["bot"] - 2.0 if buy else z["top"] + 2.0
                risk = abs(entry - stop)
                z["state"] = "done"
                trades.append({"buy": buy, "entry": entry, "risk": risk, "open": 1.0, "res": 0.0, "tp1": False, "stop_r": -1.0, "best": 0.0,
                               "i0": i, "zone": z, "fz": z["flipped"], "why": "", "new": True})
        # 2) manage open trades on this bar
        still = []
        for tr_ in trades:
            buy = tr_["buy"]; e = tr_["entry"]; risk = tr_["risk"]
            hi_r = ((h[i] - e) if buy else (e - l[i])) / risk
            close_r = ((c[i] - e) if buy else (e - c[i])) / risk
            done = False
            if not tr_["tp1"]:
                # close-based stop, as in the indicator: body close beyond SL
                if close_r <= -1.0:
                    tr_["res"] += tr_["open"] * close_r; done = True; tr_["why"] = "SL"
                elif hi_r >= 3.0 / risk and not tr_.get("new"):
                    tr_["res"] += 0.5 * (3.0 / risk); tr_["open"] = 0.5; tr_["tp1"] = True; tr_["stop_r"] = 0.0
            if not done and tr_["tp1"]:
                lo_r = ((l[i] - e) if buy else (e - h[i])) / risk
                if lo_r <= tr_["stop_r"]:
                    tr_["res"] += tr_["open"] * tr_["stop_r"]; done = True; tr_["why"] = "stop after TP1"
                elif hi_r >= 7.0 / risk and exit_mode == "close at TP":
                    tr_["res"] += tr_["open"] * (7.0 / risk); done = True; tr_["why"] = "TP"
                elif exit_mode != "close at TP":
                    tr_["best"] = max(tr_["best"], hi_r)
                    if tr_["best"] >= 7.0 / risk:
                        tr_["stop_r"] = max(tr_["stop_r"], tr_["best"] - 3.0 / risk)   # trail $3 behind the best price after TP
            tr_["new"] = False
            if not done and i - tr_["i0"] > 2000:
                tr_["res"] += tr_["open"] * close_r; done = True; tr_["why"] = "time"
            if done:
                cost = (sp[tr_["i0"]] * POINT + COMM) / risk
                rows.append({"tf": tf, "flip": flip, "exit": exit_mode, "side": "BUY" if buy else "SELL", "flipped_zone": tr_["fz"],
                             "year": str(t[i])[:4], "net_r": tr_["res"] - cost, "risk_usd": risk})
                if flip and tr_["why"] == "SL" and not tr_["fz"]:
                    z = tr_["zone"]; zones.append({"side": "SELL" if buy else "BUY", "top": z["top"], "bot": z["bot"], "born": i - 10, "flipped": True, "state": "wait"})
            else:
                still.append(tr_)
        trades = still
        if len(zones) > 400:
            zones = [z for z in zones if z["state"] == "wait"][-60:]
    return rows


if __name__ == "__main__":
    jobs = [(tf, flip, ex) for tf in ("M1", "M2", "M3", "M5") for flip in (False, True) for ex in ("close at TP", "trail $3 after TP")]
    with ProcessPoolExecutor(6) as p:
        allrows = [r for rs in p.map(run, jobs) for r in rs]
    t = pd.DataFrame(allrows)
    t.to_csv("reports/phase4/vss3_flip_trades.csv", index=False)
    out = []
    for (tf, flip, ex), g in t.groupby(["tf", "flip", "exit"]):
        yr = g.groupby("year").net_r.sum()
        w = g[g.net_r > 0]; lo = g[g.net_r <= 0]
        out.append({"tf": tf, "flip": flip, "exit": ex, "trades": len(g), "win": round((g.net_r > 0).mean() * 100, 1), "avg_win": round(w.net_r.mean(), 2),
                    "avg_loss": round(lo.net_r.mean(), 2), "net_r": round(g.net_r.sum(), 1), "buys_r": round(g[g.side == "BUY"].net_r.sum(), 1),
                    "sells_r": round(g[g.side == "SELL"].net_r.sum(), 1), "flip_trades": int(g.flipped_zone.sum()), "flip_r": round(g[g.flipped_zone].net_r.sum(), 1),
                    "avg_risk_usd": round(g.risk_usd.mean(), 2), "losing_years": int((yr <= 0).sum()),
                    **{y: round(yr.get(y, np.nan), 1) for y in map(str, range(2020, 2027))}})
    r = pd.DataFrame(out)
    r.to_csv("LebPrime AI Research/02 - Backtest Results/vss3_flip_by_year.csv", index=False)
    print(r.to_string(index=False)); print("ALL DONE")
