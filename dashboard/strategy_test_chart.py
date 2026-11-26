"""Read side of the strategy-test trade chart (display only).

The test job (scripts/strategy_test_chart.py, research sandbox) saves chart_<SYM>.npy (candles) and
chart_<SYM>.json (zones and trades) in the test folder. The dashboard only slices them: candles are
memory-mapped and served in windows of at most MAX_BARS, so a multi-year M1 test stays light.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import HTTPException

WINDOW_BARS = 3000   # default window around a trade or a scroll edge
PAD_BARS = 400       # bars shown before/after a requested time span
MAX_BARS = 20000     # most bars one request returns
PRECISION = {"XAUUSD": 2, "XAGUSD": 3, "US30": 1, "USTEC": 2, "WTI": 3, "EURUSD": 5}
TRADE_KEYS = ("id", "side", "created", "ended", "state", "reason", "limit", "entry_t", "entry", "fill", "entry2_t", "entry2", "fill2", "sl", "tp", "tp1", "tp2", "tp1_hit",
              "exit_price", "gross_r", "net_r", "stops")


def chart_symbols(folder: Path) -> list[str]:
    return sorted(p.name[len("chart_"):-len(".json")] for p in folder.glob("chart_*.json")
                  if (folder / (p.name[:-len(".json")] + ".npy")).is_file())


@lru_cache(maxsize=6)
def _load(json_path: str, mtime: float) -> dict[str, Any]:
    meta = json.loads(Path(json_path).read_text())
    zones = meta.get("zones") or []
    meta["_created"] = np.array([z["created"] for z in zones], dtype=np.int64)
    meta["_ended"] = np.array([z.get("ended") or z["created"] for z in zones], dtype=np.int64)
    meta["_trades"] = [{k: z[k] for k in TRADE_KEYS if k in z} for z in zones if z.get("trade")]
    meta["_trades"].sort(key=lambda t: t.get("entry_t") or t["created"])
    return meta


def _files(folder: Path, symbol: str) -> tuple[Path, Path]:
    symbol = str(symbol).upper()
    if symbol not in chart_symbols(folder):
        raise HTTPException(status_code=404, detail="no chart for this symbol (tests run before the chart existed have none)")
    return folder / f"chart_{symbol}.json", folder / f"chart_{symbol}.npy"


def _meta(folder: Path, symbol: str) -> tuple[dict[str, Any], np.ndarray]:
    json_path, npy_path = _files(folder, symbol)
    meta = _load(str(json_path), json_path.stat().st_mtime)
    candles = np.load(npy_path, mmap_mode="r", allow_pickle=False)
    return meta, candles


def overview(folder: Path, symbol: str) -> dict[str, Any]:
    meta, candles = _meta(folder, symbol)
    symbol = meta["symbol"]
    step = int(np.median(np.diff(candles["t"][:200]))) if len(candles) > 2 else 60
    return {"symbol": symbol, "timeframe": meta.get("timeframe"), "bars": int(len(candles)), "first": meta.get("first"),
            "last": meta.get("last"), "exit_mode": meta.get("exit_mode", "Targets"), "bar_seconds": step, "precision": PRECISION.get(symbol, 5),
            "zones": len(meta["zones"]), "trades": meta["_trades"]}


def window(folder: Path, symbol: str, t0: int | None = None, t1: int | None = None,
           i0: int | None = None, i1: int | None = None) -> dict[str, Any]:
    """Candles + zones for a window. Either a time span (t0..t1, padded) or bar indexes (i0..i1)."""
    meta, candles = _meta(folder, symbol)
    n = len(candles)
    if n == 0:
        return {"from_index": 0, "to_index": -1, "total": 0, "candles": [], "zones": []}
    if i0 is not None or i1 is not None:
        lo = max(0, int(i0 if i0 is not None else 0))
        hi = min(n - 1, int(i1 if i1 is not None else lo + WINDOW_BARS - 1))
    else:
        times = candles["t"]
        a = int(np.searchsorted(times, int(t0 if t0 is not None else times[-1]), side="left"))
        b = int(np.searchsorted(times, int(t1 if t1 is not None else (t0 if t0 is not None else times[-1])), side="right")) - 1
        b = max(a, b)
        extra = max(PAD_BARS, (WINDOW_BARS - (b - a + 1)) // 2)
        lo, hi = max(0, a - extra), min(n - 1, b + extra)
    hi = min(hi, lo + MAX_BARS - 1)
    if hi < lo:
        return {"from_index": lo, "to_index": lo - 1, "total": n, "candles": [], "zones": []}
    part = candles[lo:hi + 1]
    digits = PRECISION.get(meta["symbol"], 5)
    rows = np.column_stack([part["o"], part["h"], part["l"], part["c"]]).astype(np.float64).round(digits)
    out_candles = [[int(t), *r] for t, r in zip(part["t"].tolist(), rows.tolist())]
    first, last = int(part["t"][0]), int(part["t"][-1])
    mask = (meta["_created"] <= last) & (meta["_ended"] >= first)
    zones = [meta["zones"][i] for i in np.flatnonzero(mask)]
    return {"from_index": lo, "to_index": hi, "total": n, "candles": out_candles, "zones": zones}
