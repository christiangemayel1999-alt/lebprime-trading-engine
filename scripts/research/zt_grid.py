"""Zone parity study, step 3: exit variants on every symbol/TF. Tune on 2020-23, check on 2024-26."""
import sys, os, json
sys.path.insert(0, "/var/lib/lebprime/research/zonetrail/research")
import numpy as np, pandas as pd
import zt_sim as S
SYMS = ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]; TFS = os.environ.get("TFS", "M1,M2,M3,M5,M15,M30").split(",")
CFG = {
    "TP 1R (old)": dict(be_r=0, tmode=0, tp_r=1.0),
    "SL+BE0.5 only": dict(be_r=0.5, tmode=0),
    "HIS: BE0.5, trail from 1R, 1R behind": dict(),
    "HIS, stop moves every minute": dict(upd_min=1),
    "HIS, BE at exact entry": dict(lock_cost=0),
}
for dist in (0.25, 0.5, 0.75, 1.5): CFG[f"BE0.5, trail from 1R, {dist}R behind"] = dict(tval=dist)
for st in (0.75, 1.5, 2.0, 3.0): CFG[f"BE0.5, trail from {st}R, 0.5R behind"] = dict(tstart=st, tval=0.5)
for st in (1.5, 2.0, 3.0): CFG[f"BE0.5, trail from {st}R, 1R behind"] = dict(tstart=st, tval=1.0)
for be in (0.75, 1.0): CFG[f"BE{be}, trail from 1R, 0.5R behind"] = dict(be_r=be, tval=0.5)
CFG["no BE, trail from 1R, 0.5R behind"] = dict(be_r=0, tval=0.5)
for a in (1.0, 2.0, 3.0): CFG[f"BE0.5, trail from 1R, {a} ATR behind"] = dict(tmode=2, tval=a)
for nb in (3, 5, 10): CFG[f"BE0.5, trail from 1R, under last {nb} candles"] = dict(tmode=3, tval=nb)
CFG["BE0.5, TP 2R"] = dict(tmode=0, tp_r=2.0)
CFG["BE0.5, TP 3R"] = dict(tmode=0, tp_r=3.0)


def summ(d):
    w = d[d.net_r > 0]; ls = d[d.net_r <= 0]
    return {"n": len(d), "win_pct": 100 * len(w) / max(len(d), 1), "net_r": d.net_r.sum(), "avg_win": w.net_r.mean(), "avg_loss": ls.net_r.mean(),
            "is_r": d[d.year <= 2023].net_r.sum(), "oos_r": d[d.year >= 2024].net_r.sum()}


if __name__ == "__main__":
    rows = []; keep = {}
    for sym in SYMS:
        for tf in TFS:
            if not os.path.exists(f"{S.ZD}/{sym}_{tf}_w{S.WIN}.pkl"): print("missing", sym, tf, flush=True); continue
            for name, kw in CFG.items():
                d = S.run(sym, tf, **kw)
                r = {"config": name, "symbol": sym, "tf": tf, **summ(d)}
                for y, v in d.groupby("year").net_r.sum().items(): r[f"r{y}"] = v
                rows.append(r)
                if name in ("HIS: BE0.5, trail from 1R, 1R behind", "TP 1R (old)") or os.environ.get("KEEPALL"):
                    keep[(name, sym, tf)] = d
            print("done", sym, tf, flush=True)
    g = pd.DataFrame(rows); g.to_csv(f"{S.OUT}/grid.csv", index=False)
    pd.concat(keep.values()).to_pickle(f"{S.OUT}/trades_his_and_old.pkl")
    print("GRID DONE")
