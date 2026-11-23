"""Check each passing 2026 bull/bear/all winner on 2025 (split Sep) and the older years (two halves). Resumable."""
import json, subprocess, sys, os
import pandas as pd
DS = {("XAUUSD","H1"):52,("XAGUSD","H1"):53,("US30","H1"):54,("USTEC","H1"):55,("WTI","H1"):56,("EURUSD","H1"):57,
      ("XAUUSD","M30"):58,("XAGUSD","M30"):59,("US30","M30"):60,("USTEC","M30"):61,("WTI","M30"):62,("EURUSD","M30"):63}
for i, s in enumerate(["XAUUSD","XAGUSD","US30","USTEC","WTI","EURUSD"]):
    DS[(s,"M15")] = 124+i; DS[(s,"M5")] = 130+i; DS[(s,"M3")] = 136+i; DS[(s,"H4")] = 157+i; DS[(s,"M10")] = 163+i
for i, s in enumerate(["XAUUSD","XAGUSD","US30","USTEC"]):
    DS[(s,"M1")] = 148+i; DS[(s,"M2")] = 142+i
OLD = {"H4": ("2020-01-01..2025-01-01","2022-07-01"), "M10": ("2020-01-01..2025-01-01","2022-07-01"), "H1": ("2020-01-01..2025-01-01","2022-07-01"), "M30": ("2020-01-01..2025-01-01","2022-07-01"), "M15": ("2020-01-01..2025-01-01","2022-07-01"),
       "M5": ("2020-01-01..2025-01-01","2022-07-01"), "M3": ("2021-01-01..2025-01-01","2023-01-01"), "M2": ("2022-01-01..2025-01-01","2023-07-01"), "M1": ("2023-01-01..2025-01-01","2024-01-01")}
rules = json.load(open("reports/phase4/regime_2026_m10h4/rules.json"))
best = pd.read_csv("LebPrime AI Research/02 - Backtest Results/regime_m10h4_best.csv")
best = best[best.passes]
os.makedirs("reports/phase4/regime_val_m10h4", exist_ok=True)
for (sym, tf), g in best.groupby(["symbol", "tf"]):
    ds = DS[(sym, tf)]
    p = f"reports/phase4/regime_val_m10h4/{sym}_{tf}.json"; json.dump([rules[i] for i in g.rule], open(p, "w"))
    for per, (win, sp) in (("2025", ("2025-01-01..2026-01-01", "2025-07-01")), ("old", OLD[tf])):
        out = f"reports/phase4/regime_val_m10h4/{sym}_{tf}_{per}"
        if not os.path.exists(f"{out}/split_pooled.csv"):
            subprocess.run([sys.executable, "scripts/phase4_search.py", "split", "--window", win, "--split", sp, "--settings", p, "--datasets", str(ds), "--out", out, "--workers", "3"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(sym, tf, "done", flush=True)
print("ALL DONE", flush=True)
