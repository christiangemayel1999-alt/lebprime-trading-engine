"""Validate per-symbol candidates (positive in both halves of the last year) on older, unseen data."""
import json, subprocess, sys
import pandas as pd
tf, window, split = sys.argv[1], sys.argv[2], sys.argv[3]
t = pd.read_csv(f"reports/phase4/opt_{tf}/split_runs.csv")
g = t[(t.tune_net_r > 0) & (t.check_net_r > 0) & (t.tune_trades >= 40)].copy()
g["m"] = g[["tune_net_r", "check_net_r"]].min(axis=1)
g = g.sort_values("m", ascending=False).groupby("dataset_id").head(3)
for ds, grp in g.groupby("dataset_id"):
    path = f"reports/phase4/val_{tf}_{ds}.json"
    json.dump([json.loads(o) for o in grp.overrides], open(path, "w"))
    out = f"reports/phase4/val_{tf}/{ds}"
    subprocess.run([sys.executable, "scripts/phase4_search.py", "split", "--window", window, "--split", split, "--settings", path, "--datasets", str(ds), "--out", out, "--workers", "3"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(tf, ds, "done", flush=True)
