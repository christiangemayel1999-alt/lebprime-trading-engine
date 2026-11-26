"""Time Machine panel (Indicator Lab tab): honest odds for one zone before it is traded.

The dashboard only validates the zone and queues it as a file, exactly like the strategy tests:
<reports>/dashboard_tests/<id>/request.json with "kind": "time_machine". The research-sandbox dispatcher
runs it (scripts/strategy_test.py -> scripts/time_machine_job.py), so the dashboard never does heavy work.
The live-zone list is a read-only SELECT on the bot's database. Nothing here places, changes or cancels
orders, and every route needs the dashboard login.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.strategy_tests import MAX_WAITING, StrategyTests
from services.indicator_lab.time_machine import SYMBOLS, TIMEFRAMES, canonical_symbol

KIND = "time_machine"


class TimeMachineRequest(BaseModel):
    actor: str = "dashboard"
    symbol: str
    timeframe: str
    side: str
    entry1: float
    sl: float
    zone_top: float | None = None
    zone_bottom: float | None = None
    limit_time: str | None = None      # when the LIMIT was placed (candle open time); empty = now
    live_zone_id: str | None = None
    paths: int = 10_000
    mc_days: int = 20


def live_zones(db_path: Path, limit: int = 60) -> list[dict[str, Any]]:
    """Recent zones from the live bot's database, newest first (read-only)."""
    if not db_path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT zone_id, logical_symbol, timeframe, side, zone_bottom, zone_top, entry1, stop_loss, execution_status, theoretical_state, "
                "created_at, json_extract(metadata_json, '$.limit_time') AS limit_time FROM lebprime_live_zones "
                "WHERE entry1 IS NOT NULL AND stop_loss IS NOT NULL ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        state = str(r["theoretical_state"] or "")
        out.append({
            "zone_id": r["zone_id"], "symbol": canonical_symbol(r["logical_symbol"]), "logical_symbol": r["logical_symbol"],
            "timeframe": r["timeframe"], "side": "BUY" if str(r["side"]).upper() in {"BUY", "DEMAND"} else "SELL",
            "entry1": r["entry1"], "sl": r["stop_loss"], "zone_top": r["zone_top"], "zone_bottom": r["zone_bottom"],
            "limit_time": r["limit_time"] or r["created_at"], "state": state, "execution_status": r["execution_status"],
            "open": state in {"PENDING", "ACTIVE"} and r["execution_status"] != "HISTORICAL_REPLAY", "created_at": r["created_at"],
        })
    return out


AUTO_MAX_WAITING = 12   # automatic runs for new live zones may queue up to this many; they never block manual runs


def waiting_runs(tests: StrategyTests) -> tuple[int, int]:
    """(manual + strategy jobs, automatic Time Machine runs) that are queued or running."""
    manual = auto = 0
    for t in tests.list(limit=80, kind=None):
        if t["state"] in {"QUEUED", "RUNNING"}:
            if (t["request"] or {}).get("source") == "auto":
                auto += 1
            else:
                manual += 1
    return manual, auto


def queue_run(tests: StrategyTests, fields: dict[str, Any], source: str) -> Path:
    """Validate one zone and write its request.json (the sandbox dispatcher picks it up). Raises HTTPException on bad input."""
    symbol = canonical_symbol(fields.get("symbol", ""))
    side = str(fields.get("side", "")).upper()
    timeframe = str(fields.get("timeframe", ""))
    entry1, sl = float(fields.get("entry1") or 0), float(fields.get("sl") or 0)
    paths, mc_days = int(fields.get("paths") or 10_000), int(fields.get("mc_days") or 20)
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=400, detail=f"symbol must be one of {', '.join(SYMBOLS)}")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if not (entry1 > 0 and sl > 0) or (entry1 - sl) * (1 if side == "BUY" else -1) <= 0:
        raise HTTPException(status_code=400, detail="SL must be below Entry 1 for a buy and above it for a sell")
    if not 1000 <= paths <= 50_000 or not 5 <= mc_days <= 120:
        raise HTTPException(status_code=400, detail="paths must be 1,000-50,000 and recent days 5-120")
    limit_time = None
    if fields.get("limit_time"):
        try:
            parsed = datetime.fromisoformat(str(fields["limit_time"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="limit time must be an ISO date-time") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if not datetime(2020, 6, 1, tzinfo=timezone.utc) <= parsed <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="limit time must be between mid-2020 and now")
        limit_time = parsed.astimezone(timezone.utc).isoformat()
    manual, auto = waiting_runs(tests)
    if source == "auto" and auto >= AUTO_MAX_WAITING:
        raise HTTPException(status_code=429, detail=f"{auto} automatic runs are already waiting")
    if source != "auto" and manual >= MAX_WAITING:
        raise HTTPException(status_code=429, detail=f"{manual} jobs are already queued or running; wait for one to finish")
    run_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    folder = tests.root / run_id
    folder.mkdir(parents=True, exist_ok=False)
    request = {"id": run_id, "kind": KIND, "actor": str(fields.get("actor") or source)[:40], "symbol": symbol, "timeframe": timeframe,
               "side": side, "entry1": entry1, "sl": sl, "zone_top": fields.get("zone_top"), "zone_bottom": fields.get("zone_bottom"),
               "limit_time": limit_time, "live_zone_id": (fields.get("live_zone_id") or "")[:60] or None, "source": source,
               "zone_state": fields.get("zone_state"), "paths": paths, "mc_days": mc_days,
               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = folder / "request.json.tmp"
    tmp.write_text(json.dumps(request, indent=1))
    tmp.replace(folder / "request.json")
    return folder


def _pct(v: Any) -> str:
    return "–" if v is None else f"{float(v):.0f}%"


def _r(v: Any) -> str:
    return "–" if v is None else f"{float(v):+.2f}R"


def headline(summary: dict[str, Any], exit_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    block = (summary.get("exits") or {}).get(exit_name) or ({"analogs": summary.get("analogs"), "monte_carlo": summary.get("monte_carlo")} if exit_name == "tp" else {})
    analogs = block.get("analogs") or {}
    head = next((t for t in analogs.get("tiers", []) if t.get("key") == analogs.get("headline")), {}) or {}
    return head, block.get("monte_carlo") or {}


def verdict(head: dict[str, Any], mc: dict[str, Any]) -> str:
    if not head.get("trades"):
        return "⚪ No history for zones like this yet"
    if not head.get("trusted"):
        return f"⚠️ Only {head['trades']} similar zones: a hint, not a verdict"
    if (head.get("avg_r") or 0) > 0 and (mc.get("expected_r") or 0) > 0:
        return "✅ Positive odds"
    if (head.get("avg_r") or 0) > 0:
        return "🟡 Past zones positive, recent price paths negative"
    return "❌ Negative odds"


def telegram_message(summary: dict[str, Any]) -> tuple[str, list[str]]:
    """Title + 4 lines for TelegramNotifier._send (it shows at most 4 lines)."""
    z, f = summary.get("zone") or {}, summary.get("features") or {}
    live = summary.get("live_exit") or "tp"
    other = "tp" if live == "trail" else "trail"
    title = f"🔮 Zone odds · {z.get('symbol')} {z.get('timeframe')} {z.get('side')}"
    placed = str(z.get("decision_time") or "")[11:16]
    lines = [f"E1 {z.get('entry1')} · SL {z.get('sl')} · {z.get('live_zone_id') or 'typed zone'} · LIMIT live from {placed} UTC"]
    for name in (live, other):
        head, mc = headline(summary, name)
        if name not in (summary.get("exits") or {}) and name != "tp":
            continue
        tag = "Bot exit" if name == live else "Alt exit"
        kind = "trail" if name == "trail" else "TP 1R"
        past = f"{head.get('trades', 0)} similar past zones {_pct(head.get('win_pct'))} win {_r(head.get('avg_r'))}/trade" if head.get("trades") else "no similar past zones"
        paths = (f"paths {_pct(mc.get('win_pct'))} win {_r(mc.get('expected_r'))}" if name == "trail"
                 else f"paths TP first {_pct(mc.get('p_tp_first'))} (need {_pct(mc.get('breakeven_win_pct'))})")
        lines.append(f"{tag} ({kind}): {past} · {paths}")
    head, mc = headline(summary, live)
    extra = [verdict(head, mc)]
    if summary.get("p_fill") is not None:
        extra.append(f"fill chance {_pct(summary['p_fill'])}")
    sl_atr = f.get("sl_atr")
    extra.append(f"H1 trend {f.get('h1_trend')} · {f.get('session')} · SL {float(sl_atr):.2f} ATR" if sl_atr is not None else f"H1 trend {f.get('h1_trend')} · {f.get('session')}")
    lines.append(" · ".join(extra))
    return title, lines[:4]


def create_time_machine_router(base_dir: Path, auth_dependency: Callable[..., Any], audit_service: Any = None, db_path: Path | None = None,
                               dispatch_state: Path = Path("/var/lib/lebprime-dispatch"),
                               mt5_store: Path = Path("/var/lib/lebprime/mt5_data/M1"),
                               watcher_status: Callable[[], dict[str, Any]] | None = None) -> APIRouter:
    tests = StrategyTests(Path(base_dir) / "reports", dispatch_state, mt5_store)
    router = APIRouter(dependencies=[Depends(auth_dependency)])

    @router.get("/api/time-machine")
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return {"ok": True, "runs": tests.list(max(1, min(int(limit), 100)), kind=KIND), "dispatcher_alive": tests.dispatcher_alive(),
                "symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES), "data_until": tests.data_coverage(),
                "watcher": watcher_status() if watcher_status else None}

    @router.get("/api/time-machine/live-zones")
    def get_live_zones(limit: int = 60) -> dict[str, Any]:
        return {"ok": True, "zones": live_zones(db_path, max(1, min(int(limit), 200))) if db_path else []}

    @router.post("/api/time-machine")
    def queue(payload: TimeMachineRequest) -> dict[str, Any]:
        folder = queue_run(tests, payload.model_dump(), source="live" if payload.live_zone_id else "manual")
        if audit_service is not None:
            try:
                audit_service.log_action(payload.actor, "time_machine_queued", folder.name, {"symbol": payload.symbol, "timeframe": payload.timeframe})
            except Exception:  # auditing must never block a run
                pass
        return {"ok": True, "run": tests.describe(folder), "dispatcher_alive": tests.dispatcher_alive()}

    @router.get("/api/time-machine/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        folder = tests.folder(run_id)
        if tests._json(folder / "request.json").get("kind") != KIND:
            raise HTTPException(status_code=404, detail="unknown run")
        return {"ok": True, "run": tests.describe(folder, full=True)}

    @router.post("/api/time-machine/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        return {"ok": True, "run": tests.cancel(run_id)}

    return router
