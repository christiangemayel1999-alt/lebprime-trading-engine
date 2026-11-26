"""Strategy tests from the Indicator Lab tab, run in the capped research sandbox.

The dashboard never runs a backtest itself. POST /api/strategy-tests validates the request and writes
<reports>/dashboard_tests/<id>/request.json. The root dispatcher (lebprime-research-dispatch.timer)
starts scripts/strategy_test.py for it with `lebprime-research --detach`, one test at a time, and keeps
its bookkeeping in /var/lib/lebprime-dispatch. Nothing here touches MT5, the live bot, its config or
its controls. Every route needs the dashboard login.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dashboard import strategy_test_chart
from dashboard.strategy_test_chart import chart_symbols

SYMBOLS = ("XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD")
TIMEFRAMES = ("M1", "M2", "M3", "M5", "M10", "M15", "M30", "H1", "H4")
MAX_WAITING = 3
ID_LEN = len("20260101-000000-abcdef")
DISPATCH_STALE_SECONDS = 120


class StrategyTestRequest(BaseModel):
    actor: str = "dashboard"
    symbols: list[str] = Field(min_length=1, max_length=len(SYMBOLS))
    timeframe: str
    start: str
    end: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    name: str = ""


class StrategyTests:
    def __init__(self, reports_root: Path, dispatch_state: Path, mt5_store: Path) -> None:
        self.root = reports_root / "dashboard_tests"
        self.dispatch = dispatch_state
        self.mt5_store = mt5_store

    # ------------------------------------------------------------ helpers
    def folder(self, test_id: str) -> Path:
        if len(test_id) != ID_LEN or not all(c.isalnum() or c == "-" for c in test_id):
            raise HTTPException(status_code=404, detail="unknown test")
        path = self.root / test_id
        if not (path / "request.json").is_file():
            raise HTTPException(status_code=404, detail="unknown test")
        return path

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _dispatch_file(self, test_id: str, suffix: str) -> str | None:
        try:
            return (self.dispatch / f"{test_id}.{suffix}").read_text().strip()
        except OSError:
            return None

    def state(self, test_id: str, status: dict[str, Any]) -> str:
        done = self._dispatch_file(test_id, "done")
        job_state = status.get("state")
        if done is not None:
            word = done.split()[0] if done else "unknown"
            if word == "canceled":
                return "CANCELED"
            if job_state in {"COMPLETED", "FAILED"}:
                return job_state
            return "FAILED"  # the job ended without writing a result (killed, out of memory, start failed)
        if self._dispatch_file(test_id, "unit") is not None:
            return "RUNNING"
        if (self.root / test_id / "cancel").exists():
            return "CANCELING"
        return "QUEUED"

    def describe(self, folder: Path, full: bool = False) -> dict[str, Any]:
        test_id = folder.name
        request = self._json(folder / "request.json")
        status = self._json(folder / "status.json")
        item: dict[str, Any] = {
            "id": test_id, "state": self.state(test_id, status), "request": request,
            "started_at": status.get("started_at"), "updated_at": status.get("updated_at"),
            "error": status.get("error"),
        }
        summary = self._json(folder / "summary.json") if (folder / "summary.json").is_file() else {}
        if summary:
            item["total"] = (summary.get("symbols", {}).get("ALL") or {}).get("total")
            if "analogs" in summary:  # Time Machine run: headline odds for the list
                analogs = summary.get("analogs") or {}
                head = next((t for t in analogs.get("tiers", []) if t.get("key") == analogs.get("headline")), {})
                item["odds"] = {"trades": head.get("trades"), "win_pct": head.get("win_pct"), "avg_r": head.get("avg_r"),
                                "mc_tp_pct": (summary.get("monte_carlo") or {}).get("p_tp_first")}
        if full:
            item["progress"] = status.get("progress", [])
            item["summary"] = summary or None
            item["has_trades"] = (folder / "trades.csv").is_file()
            item["chart_symbols"] = chart_symbols(folder)
            item["traceback"] = status.get("traceback")
        return item

    def list(self, limit: int = 30, kind: str | None = "strategy") -> list[dict[str, Any]]:
        """Newest first. kind "strategy" = strategy tests, "time_machine" = Time Machine runs, None = every queued job."""
        if not self.root.is_dir():
            return []
        folders = sorted((p for p in self.root.iterdir() if p.is_dir() and (p / "request.json").is_file()), reverse=True)
        out = []
        for folder in folders:
            if len(out) >= limit:
                break
            item = self.describe(folder)
            if kind is None or (item["request"].get("kind") or "strategy") == kind:
                out.append(item)
        return out

    def dispatcher_alive(self) -> bool:
        try:
            return time.time() - self.dispatch.stat().st_mtime < DISPATCH_STALE_SECONDS or any(
                time.time() - p.stat().st_mtime < DISPATCH_STALE_SECONDS for p in self.dispatch.iterdir())
        except OSError:
            return False

    def data_coverage(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for symbol in SYMBOLS:
            path = self.mt5_store / f"{symbol}.csv"
            try:
                with path.open("rb") as handle:
                    handle.seek(max(0, path.stat().st_size - 200))
                    last = handle.read().decode(errors="replace").strip().splitlines()[-1]
                out[symbol] = last.split(",")[0]
            except (OSError, IndexError):
                out[symbol] = None
        return out

    # ------------------------------------------------------------ create
    def create(self, payload: StrategyTestRequest) -> dict[str, Any]:
        from services.indicator_lab.parameter_registry import validate_parameters

        symbols = []
        for symbol in payload.symbols:
            symbol = str(symbol).upper()
            if symbol not in SYMBOLS:
                raise HTTPException(status_code=400, detail=f"symbol must be one of {', '.join(SYMBOLS)}")
            if symbol not in symbols:
                symbols.append(symbol)
        if payload.timeframe not in TIMEFRAMES:
            raise HTTPException(status_code=400, detail=f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        try:
            start, end = date.fromisoformat(payload.start[:10]), date.fromisoformat(payload.end[:10])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start and end must be dates (YYYY-MM-DD)") from exc
        if not (date(2015, 1, 1) <= start < end):
            raise HTTPException(status_code=400, detail="start must be before end")
        try:
            validate_parameters("LEBPRIME", payload.parameters)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        waiting = [t for t in self.list(limit=50, kind=None) if t["state"] in {"QUEUED", "RUNNING"}]
        if len(waiting) >= MAX_WAITING:
            raise HTTPException(status_code=429, detail=f"{len(waiting)} tests are already queued or running; wait for one to finish")
        test_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        folder = self.root / test_id
        folder.mkdir(parents=True, exist_ok=False)
        request = {"id": test_id, "name": payload.name[:80], "actor": payload.actor[:40], "symbols": symbols,
                   "timeframe": payload.timeframe, "start": start.isoformat(), "end": end.isoformat(),
                   "parameters": payload.parameters, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = folder / "request.json.tmp"
        tmp.write_text(json.dumps(request, indent=1))
        tmp.replace(folder / "request.json")
        return self.describe(folder)

    def cancel(self, test_id: str) -> dict[str, Any]:
        folder = self.folder(test_id)
        (folder / "cancel").touch()
        return self.describe(folder)


def create_strategy_test_router(base_dir: Path, auth_dependency: Callable[..., Any], audit_service: Any = None,
                                dispatch_state: Path = Path("/var/lib/lebprime-dispatch"),
                                mt5_store: Path = Path("/var/lib/lebprime/mt5_data/M1")) -> APIRouter:
    tests = StrategyTests(Path(base_dir) / "reports", dispatch_state, mt5_store)
    router = APIRouter(dependencies=[Depends(auth_dependency)])

    def audit(actor: str, action: str, target: str, details: dict[str, Any]) -> None:
        if audit_service is not None:
            try:
                audit_service.log_action(actor, action, target, details)
            except Exception:  # auditing must never block a test
                pass

    @router.get("/api/strategy-tests")
    def list_tests(limit: int = 30) -> dict[str, Any]:
        return {"ok": True, "tests": tests.list(max(1, min(int(limit), 100))), "dispatcher_alive": tests.dispatcher_alive(),
                "symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES), "data_until": tests.data_coverage()}

    @router.post("/api/strategy-tests")
    def create_test(payload: StrategyTestRequest) -> dict[str, Any]:
        item = tests.create(payload)
        audit(payload.actor, "strategy_test_queued", item["id"], {"symbols": item["request"]["symbols"], "timeframe": payload.timeframe})
        return {"ok": True, "test": item, "dispatcher_alive": tests.dispatcher_alive()}

    @router.get("/api/strategy-tests/{test_id}")
    def get_test(test_id: str) -> dict[str, Any]:
        return {"ok": True, "test": tests.describe(tests.folder(test_id), full=True)}

    @router.post("/api/strategy-tests/{test_id}/cancel")
    def cancel_test(test_id: str) -> dict[str, Any]:
        item = tests.cancel(test_id)
        audit("dashboard", "strategy_test_cancel", test_id, {})
        return {"ok": True, "test": item}

    @router.get("/api/strategy-tests/{test_id}/trades.csv")
    def trades_csv(test_id: str) -> FileResponse:
        path = tests.folder(test_id) / "trades.csv"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no trades file yet")
        return FileResponse(path, media_type="text/csv", filename=f"strategy_test_{test_id}.csv")

    @router.get("/api/strategy-tests/{test_id}/chart")
    def chart_overview(test_id: str, symbol: str) -> dict[str, Any]:
        """Trade list + candle range of one symbol of a finished test (display only)."""
        return {"ok": True, **strategy_test_chart.overview(tests.folder(test_id), symbol)}

    @router.get("/api/strategy-tests/{test_id}/chart/bars")
    def chart_bars(test_id: str, symbol: str, t0: int | None = None, t1: int | None = None,
                   i0: int | None = None, i1: int | None = None) -> dict[str, Any]:
        """Candles and the zones that overlap them, for a time span (t0..t1) or bar indexes (i0..i1)."""
        return {"ok": True, **strategy_test_chart.window(tests.folder(test_id), symbol, t0, t1, i0, i1)}

    return router
