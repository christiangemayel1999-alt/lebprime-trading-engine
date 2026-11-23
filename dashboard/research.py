"""Read-only Research page: browse backtest result files and see per-setting statistics.

Everything here only reads files under <project>/reports (on the VPS: /var/lib/lebprime/reports).
No route writes anything, and none touches the trading controls, config or the broker.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

ALLOWED_SUFFIXES = {".csv", ".txt", ".json", ".md", ".log"}
MAX_TRADES_FILE_BYTES = 400 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_GROUPS = 500
EQUITY_POINTS = 400
GROUP_COLUMNS = ("symbol", "tf", "timeframe", "setting", "label", "variant", "name")
TIME_COLUMNS = ("exit_time", "close_time", "entry_time", "time", "timestamp", "date")
SEP = " | "


class ResearchReports:
    """Lists report files and computes trade statistics, with a small (path, mtime) cache."""

    def __init__(self, reports_root: Path) -> None:
        self.root = reports_root
        self._cache: dict[tuple[str, float], dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- paths
    def resolve(self, relative: str) -> Path:
        root = self.root.resolve()
        candidate = (root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="path outside the reports folder")
        if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(status_code=404, detail="report file not found")
        return candidate

    @staticmethod
    def _header(path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return [c.strip().lower() for c in handle.readline().strip().split(",")]
        except OSError:
            return []

    def is_trades_file(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        header = self._header(path)
        return "net_r" in header and (any(c in header for c in TIME_COLUMNS) or "year" in header)

    def list_files(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        root = self.root.resolve()
        items = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            stat = path.stat()
            items.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": stat.st_size,
                    "modified": stat.st_mtime,
                    "kind": "trades" if self.is_trades_file(path) else path.suffix.lower().lstrip("."),
                }
            )
        items.sort(key=lambda item: item["modified"], reverse=True)
        return items

    # ---------------------------------------------------------------- statistics
    def _load_trades(self, path: Path) -> pd.DataFrame:
        if path.stat().st_size > MAX_TRADES_FILE_BYTES:
            raise HTTPException(status_code=413, detail="file too large to analyse here")
        header = self._header(path)
        wanted = [c for c in header if c in GROUP_COLUMNS or c in TIME_COLUMNS or c in {"net_r", "side", "year"}]
        frame = pd.read_csv(path, usecols=lambda c: c.strip().lower() in wanted)
        frame.columns = [c.strip().lower() for c in frame.columns]
        frame["net_r"] = pd.to_numeric(frame["net_r"], errors="coerce")
        frame = frame.dropna(subset=["net_r"])
        time_col = next((c for c in TIME_COLUMNS if c in frame.columns), None)
        if time_col:
            frame["_time"] = pd.to_datetime(frame[time_col], errors="coerce", utc=True, format="ISO8601")
            frame = frame.sort_values("_time", kind="stable")
            frame["_year"] = frame["_time"].dt.year
        if "year" in frame.columns:
            frame["_year"] = pd.to_numeric(frame["year"], errors="coerce")
        if "_year" not in frame.columns:
            frame["_year"] = pd.NA
        group_cols = [c for c in GROUP_COLUMNS if c in frame.columns]
        if group_cols:
            key = frame[group_cols[0]].astype(str)
            for col in group_cols[1:]:
                key = key + SEP + frame[col].astype(str)
            frame["_group"] = key
        else:
            frame["_group"] = "all trades"
        if "side" in frame.columns:
            first = frame["side"].astype(str).str.strip().str.upper().str[:1]
            frame["_side"] = first.map({"B": "BUY", "L": "BUY", "S": "SELL"}).fillna("?")
        else:
            frame["_side"] = "?"
        return frame

    @staticmethod
    def _basic(series: pd.Series) -> dict[str, Any]:
        trades = int(series.size)
        if trades == 0:
            return {"trades": 0, "win_rate": None, "net_r": 0.0, "avg_r": None, "max_dd_r": 0.0}
        equity = series.cumsum()
        drawdown = float((equity.cummax().clip(lower=0) - equity).max())
        return {
            "trades": trades,
            "win_rate": round(float((series > 0).mean() * 100), 2),
            "net_r": round(float(series.sum()), 2),
            "avg_r": round(float(series.mean()), 3),
            "max_dd_r": round(drawdown, 2),
        }

    def _analysis(self, path: Path) -> dict[str, Any]:
        key = (str(path), path.stat().st_mtime)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        frame = self._load_trades(path)
        groups: dict[str, dict[str, Any]] = {}
        for name, part in frame.groupby("_group", sort=False):
            r = part["net_r"]
            summary = self._basic(r)
            summary["group"] = str(name)
            summary["buys"] = self._basic(part.loc[part["_side"] == "BUY", "net_r"])
            summary["sells"] = self._basic(part.loc[part["_side"] == "SELL", "net_r"])
            years = []
            for year, ypart in part.dropna(subset=["_year"]).groupby("_year"):
                stats = self._basic(ypart["net_r"])
                stats["year"] = int(year)
                years.append(stats)
            summary["years"] = years
            summary["profitable_years"] = sum(1 for y in years if y["net_r"] > 0)
            equity = r.cumsum().reset_index(drop=True)
            step = max(1, len(equity) // EQUITY_POINTS)
            points = equity.iloc[::step].round(3).tolist()
            if len(equity) and (len(equity) - 1) % step:
                points.append(round(float(equity.iloc[-1]), 3))
            summary["equity"] = points
            groups[str(name)] = summary
        result = {"groups": groups, "total_groups": len(groups)}
        with self._lock:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[key] = result
        return result

    def trade_summary(self, relative: str) -> dict[str, Any]:
        path = self.resolve(relative)
        if not self.is_trades_file(path):
            raise HTTPException(status_code=400, detail="not a trades file (needs net_r and a time or year column)")
        analysis = self._analysis(path)
        rows = sorted(analysis["groups"].values(), key=lambda g: g["net_r"], reverse=True)[:MAX_GROUPS]
        light = [{k: v for k, v in row.items() if k not in {"equity", "years"}} for row in rows]
        return {"path": relative, "total_groups": analysis["total_groups"], "groups": light}

    def trade_detail(self, relative: str, group: str) -> dict[str, Any]:
        path = self.resolve(relative)
        analysis = self._analysis(path)
        detail = analysis["groups"].get(group)
        if detail is None:
            raise HTTPException(status_code=404, detail="setting not found in this file")
        return {"path": relative, **detail}

    def preview(self, relative: str, rows: int = 200) -> dict[str, Any]:
        path = self.resolve(relative)
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path, nrows=max(1, min(rows, 1000)))
            return {"path": relative, "type": "table", "columns": [str(c) for c in frame.columns],
                    "rows": json.loads(frame.to_json(orient="values", date_format="iso"))}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_TEXT_BYTES)
        return {"path": relative, "type": "text", "text": text, "truncated": path.stat().st_size > MAX_TEXT_BYTES}


def create_research_router(base_dir: Path, auth_dependency: Callable[..., Any], templates: Any) -> APIRouter:
    """Read-only routes. Every route requires the dashboard login."""
    router = APIRouter()
    reports = ResearchReports(Path(base_dir) / "reports")

    @router.get("/research", response_class=HTMLResponse)
    async def research_page(request: Request, _: None = Depends(auth_dependency)) -> HTMLResponse:
        return templates.TemplateResponse(request, "research.html", {})

    @router.get("/api/research/files")
    async def research_files(_: None = Depends(auth_dependency)) -> dict[str, Any]:
        return {"files": reports.list_files()}

    @router.get("/api/research/trades")
    async def research_trades(path: str = Query(...), _: None = Depends(auth_dependency)) -> dict[str, Any]:
        return reports.trade_summary(path)

    @router.get("/api/research/trades/detail")
    async def research_trade_detail(path: str = Query(...), group: str = Query(...), _: None = Depends(auth_dependency)) -> dict[str, Any]:
        return reports.trade_detail(path, group)

    @router.get("/api/research/preview")
    async def research_preview(path: str = Query(...), rows: int = Query(200), _: None = Depends(auth_dependency)) -> dict[str, Any]:
        return reports.preview(path, rows)

    return router
