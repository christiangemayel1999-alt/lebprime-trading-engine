import sys, os
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
from concurrent.futures import ProcessPoolExecutor
from final_review import pick_zone, job
if __name__ == "__main__":
    p = pick_zone()
    with ProcessPoolExecutor(2) as ex:
        for s, tf, m in ex.map(job, [("XAUUSD", tf, p[("XAUUSD", tf)][0]) for tf in ("M1", "M3")]):
            print("done", s, tf, m and m["net_r"], flush=True)
    print("ALL DONE")
