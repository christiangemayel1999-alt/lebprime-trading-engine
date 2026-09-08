"""FastAPI routes for the professional trading bot dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from dashboard.schemas import (
    BacktestRunRequest,
    ConfigPatchRequest,
    ExecutionModeRequest,
    FamilyToggleRequest,
    JobBacktestRequest,
    JobCancelRequest,
    JobOOSRequest,
    JobResumeRequest,
    ManualTradeOpenRequest,
    ModeSwitchRequest,
    ModifyPositionRequest,
    PartialCloseRequest,
    PositionTicketRequest,
    PresetRequest,
    RawConfigImportRequest,
    OOSRebuildRequest,
    OOSResumeRequest,
    OOSRunRequest,
    StrategyToggleRequest,
    ToggleRequest,
)
from trading_bot.core.execution_mode import dashboard_mode_options


def create_router(services: dict[str, Any], templates: Jinja2Templates) -> APIRouter:
    """Build the dashboard router with injected services."""
    router = APIRouter()
    config_manager = services["config_manager"]
    control_state = services["control_state"]
    control_plane = services["control_plane"]
    bot_control = services["bot_control"]
    operator_service = services["operator_service"]
    manual_trade_service = services["manual_trade_service"]
    dashboard_data = services["dashboard_data"]
    database = services["database"]
    audit_service = services["audit_service"]
    dashboard_auth = services["dashboard_auth"]
    oos_service = services.get("oos_service")
    job_service = services.get("job_service")
    base_dir = Path(services["base_dir"])

    def assert_not_readonly() -> None:
        config = config_manager.dashboard_view()
        if bool(config.get("dashboard", {}).get("readonly_mode", False)):
            raise HTTPException(status_code=403, detail="Dashboard is in readonly mode")

    def worker_status_snapshot(config: dict[str, Any]) -> dict[str, Any]:
        stale_seconds = int(config.get("dashboard", {}).get("worker_stale_seconds", 30))
        if hasattr(dashboard_data, "worker_status"):
            return dashboard_data.worker_status(stale_seconds=stale_seconds)
        return {
            "ok": True,
            "available": False,
            "alive": False,
            "stale": False,
            "state": "unavailable",
            "status": "unavailable",
            "message": "Worker status service not configured",
        }

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request, _: None = Depends(dashboard_auth.dependency)) -> HTMLResponse:
        config = config_manager.dashboard_view()
        status = control_state.summarize(stale_after_seconds=int(config["dashboard"]["stale_heartbeat_seconds"]))
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "initial_config": json.dumps(config),
                "initial_status": json.dumps(status),
                "dashboard_auth": json.dumps(dashboard_auth.public_status()),
            },
        )

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "trading-dashboard", "base_dir": str(base_dir)}

    @router.get("/api/config")
    async def get_config(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        view = control_plane.get_config()
        return {"ok": True, **view}

    @router.post("/api/config/preview")
    async def preview_config(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return control_plane.update_config(
            actor=payload.actor,
            updates=payload.updates,
            reason=payload.reason,
            target="config",
            preview_only=True,
        )

    @router.post("/api/config")
    async def update_config(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(
            actor=payload.actor,
            updates=payload.updates,
            reason=payload.reason,
            target="config",
            preview_only=payload.preview_only,
        )

    @router.post("/api/config/import")
    async def import_config(payload: RawConfigImportRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        before = config_manager.load_raw()
        imported = config_manager.import_text(payload.config_json)
        changes = config_manager.diff(before, imported)
        apply_plan = config_manager.classify_changes(changes)
        result = config_manager.save(imported, actor=payload.actor, reason="config_import")
        audit_service.log_action(payload.actor, "config_import", "config.json", {"changes": changes, "apply_plan": apply_plan})
        if apply_plan["applied_immediately"]:
            bot_control.request_config_reload(payload.actor)
        if apply_plan["pending_restart"]:
            control_state.update(
                {
                    "pending_restart": True,
                    "restart_required_reasons": [change["path"] for change in apply_plan["restart_required"]],
                }
            )
        control_state.update({"runtime": {"mode": result["config"]["bot"]["trading_mode"]}})
        return {**result, "changes": changes, "apply_plan": apply_plan}

    @router.get("/api/config/export")
    async def export_config(_: None = Depends(dashboard_auth.dependency)) -> PlainTextResponse:
        return PlainTextResponse(config_manager.export(), media_type="application/json")

    @router.post("/api/config/preset")
    async def preset_config(payload: PresetRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            return operator_service.apply_preset(payload.actor, payload.preset_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/config/reload")
    async def reload_config(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return bot_control.request_config_reload(payload.actor)

    @router.post("/api/config/clear-pending-restart")
    async def clear_pending_restart(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.clear_pending_restart(payload.actor)

    @router.get("/api/status")
    async def get_status(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        config = config_manager.dashboard_view()
        db_path = base_dir / config["storage"]["database_path"]
        control_summary = control_state.summarize(stale_after_seconds=int(config["dashboard"]["stale_heartbeat_seconds"]))
        worker_status = worker_status_snapshot(config)
        source_snapshot = getattr(config_manager, "source_of_truth_snapshot", None)
        if callable(source_snapshot):
            source_of_truth = source_snapshot(runtime_state=control_summary)
        else:
            resolved_runtime = (
                config_manager.resolved_runtime_snapshot(config)
                if callable(getattr(config_manager, "resolved_runtime_snapshot", None))
                else config.get("resolved_runtime", {})
            )
            source_of_truth = {
                "precedence": ["process_runtime", "persisted_control_state", "root_config", "storage_config"],
                "effective": {
                    "mode": str((control_summary.get("runtime") or {}).get("mode") or config.get("bot", {}).get("trading_mode", "")),
                    "database_path": str(config.get("storage", {}).get("database_path", "")),
                    "execution_enabled": bool(config.get("bot", {}).get("allow_live_execution", False)),
                    "resolved_runtime": resolved_runtime if isinstance(resolved_runtime, dict) else {},
                },
                "sources": {"mode": "fallback", "database_path": "fallback", "execution_enabled": "fallback"},
                "raw": {"config": config, "control": control_summary},
                "drift": {"detected": False},
            }
        try:
            data_snapshot = dashboard_data.status_snapshot(db_path, int(config["dashboard"]["stale_heartbeat_seconds"]), config=config)
        except Exception as exc:
            data_snapshot = {
                "metrics": {
                    "bot_status": "ERROR",
                    "last_heartbeat_time": "N/A",
                    "balance": 0.0,
                    "equity": 0.0,
                    "open_positions": 0,
                    "today_pnl": 0.0,
                    "today_trades": 0,
                    "win_rate": 0.0,
                    "today_wins": 0,
                    "today_losses": 0,
                    "total_wins": 0,
                    "total_losses": 0,
                    "total_breakevens": 0,
                    "spread": 0.0,
                    "trend": "N/A",
                    "setup": "N/A",
                    "symbol": "N/A",
                    "enabled_strategy_count": 0,
                    "enabled_family_count": 0,
                    "daily_loss_used_pct": 0.0,
                },
                "resolved_runtime": {},
                "last_signal": {},
                "last_error": {"message": str(exc), "source": "status_snapshot"},
                "recent_trades": [],
                "recent_signals": [],
                "recent_events": [],
                "daily_pnl_curve": [],
                "rejection_summary": [],
                "blocked_setups": [],
                "valid_not_executed": [],
                "family_block_matrix": [],
                "strategy_block_matrix": [],
                "unresolved_trades": [],
                "recent_closed_trades": [],
                "daily_performance": {"summary": {}, "performance_rows": []},
                "backtest_runs": [],
                "live_integrity": {},
            }
        return {
            "ok": True,
            "control": control_summary,
            "runtime": data_snapshot,
            "config_mode": config["bot"]["trading_mode"],
            "execution_mode": config.get("mode", config.get("bot", {}).get("execution_mode_summary", {})),
            "source_of_truth": source_of_truth,
            "readonly_mode": bool(config.get("dashboard", {}).get("readonly_mode", False)),
            "auth": dashboard_auth.public_status(),
            "worker": worker_status,
            "oos": oos_service.get_status() if oos_service is not None else {"ok": True, "run": None, "history": [], "state": "IDLE"},
            "jobs": {"ok": True, "jobs": job_service.list_jobs(limit=10) if job_service is not None else []},
        }

    @router.get("/api/worker/status")
    async def get_worker_status(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        config = config_manager.dashboard_view()
        return {
            "ok": True,
            "worker": worker_status_snapshot(config),
        }

    @router.get("/api/mode")
    async def get_execution_mode(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        section = control_plane.get_section("mode")
        return {
            "ok": True,
            "mode": section.get("mode", {}),
            "options": section.get("options") or dashboard_mode_options(),
        }

    @router.post("/api/mode")
    async def update_execution_mode(payload: ExecutionModeRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            result = operator_service.switch_execution_mode(payload.actor, payload.execution_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        config = config_manager.dashboard_view()
        return {
            "ok": True,
            "message": f"Execution mode updated to {payload.execution_mode}",
            **result,
            "mode": config.get("mode", config.get("bot", {}).get("execution_mode_summary", {})),
            "options": control_plane.get_section("mode").get("options", []) or dashboard_mode_options(),
        }

    @router.get("/api/runtime")
    async def get_runtime(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("runtime")}

    @router.post("/api/runtime/pause")
    async def runtime_pause(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.pause_execution(payload.actor)

    @router.post("/api/runtime/resume")
    async def runtime_resume(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.resume_execution(payload.actor)

    @router.post("/api/runtime/mode")
    async def runtime_mode(payload: ModeSwitchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            return operator_service.switch_mode(payload.actor, payload.trading_mode, payload.confirmation_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/runtime/live-enabled")
    async def runtime_live_enabled(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.set_auto_execution(payload.actor, payload.enabled)

    @router.post("/api/runtime/new-entries")
    async def runtime_new_entries(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.set_signal_generation(payload.actor, payload.enabled)

    @router.get("/api/strategies")
    async def get_strategies(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("strategies")}

    @router.patch("/api/strategies/settings")
    @router.post("/api/strategies/settings")
    async def update_strategy_settings(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(actor=payload.actor, updates=payload.updates, reason=payload.reason, target="strategy", preview_only=payload.preview_only)

    @router.get("/api/risk")
    async def get_risk(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("risk")}

    @router.patch("/api/risk/update")
    @router.post("/api/risk/update")
    async def update_risk(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(actor=payload.actor, updates=payload.updates, reason=payload.reason, target="risk", preview_only=payload.preview_only)

    @router.get("/api/execution")
    async def get_execution(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("execution")}

    @router.patch("/api/execution/update")
    @router.post("/api/execution/update")
    async def update_execution(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(actor=payload.actor, updates=payload.updates, reason=payload.reason, target="execution", preview_only=payload.preview_only)

    @router.get("/api/sessions")
    async def get_sessions(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("sessions")}

    @router.patch("/api/sessions/update")
    @router.post("/api/sessions/update")
    async def update_sessions(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(actor=payload.actor, updates=payload.updates, reason=payload.reason, target="sessions", preview_only=payload.preview_only)

    @router.get("/api/symbols")
    async def get_symbols(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, **control_plane.get_section("symbols")}

    @router.patch("/api/symbols/update")
    @router.post("/api/symbols/update")
    async def update_symbols(payload: ConfigPatchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return control_plane.update_config(actor=payload.actor, updates=payload.updates, reason=payload.reason, target="symbols", preview_only=payload.preview_only)

    @router.get("/api/backtests")
    async def get_backtests(limit: int = 20, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "backtest": control_plane.get_section("backtest").get("backtest", {}), "runs": dashboard_data.list_backtest_runs(limit=limit)}

    @router.get("/api/logs")
    async def get_logs(
        level: str | None = None,
        source: str | None = None,
        search: str | None = None,
        date_from: str | None = None,
        limit: int = 100,
        _: None = Depends(dashboard_auth.dependency),
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "logs": dashboard_data.recent_logs(level=level, source=source, search=search, date_from=date_from, limit=limit),
        }

    @router.get("/api/audit")
    async def get_audit(limit: int = 50, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "audit": dashboard_data.recent_audit(limit=limit)}

    @router.post("/api/families/toggle")
    async def toggle_family(payload: FamilyToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            return operator_service.toggle_family(payload.actor, payload.family_name, payload.enabled, payload.live_allowed)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith("Unknown family") else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.post("/api/strategies/toggle")
    async def toggle_strategy(payload: StrategyToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            return operator_service.toggle_strategy(payload.actor, payload.strategy_name, payload.enabled)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith("Unknown strategy") else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.post("/api/control/start")
    async def start_bot(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.start_bot(payload.actor)

    @router.post("/api/control/stop")
    async def stop_bot(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.stop_bot(payload.actor, force=bool(payload.reason == "force"))

    @router.post("/api/control/pause")
    async def pause_execution(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.pause_execution(payload.actor)

    @router.post("/api/control/resume")
    async def resume_execution(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.resume_execution(payload.actor)

    @router.post("/api/control/kill-switch")
    async def kill_switch(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.set_kill_switch(payload.actor, payload.enabled, payload.reason)

    @router.post("/api/control/emergency-stop")
    async def emergency_stop(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        reason = payload.reason or "dashboard_emergency_stop"
        return operator_service.set_kill_switch(payload.actor, True, reason)

    @router.post("/api/control/auto-execution")
    async def auto_execution(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return operator_service.set_auto_execution(payload.actor, payload.enabled)

    @router.post("/api/control/readonly")
    async def readonly_mode(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return operator_service.set_readonly(payload.actor, payload.enabled)

    @router.post("/api/control/mode")
    async def switch_mode(payload: ModeSwitchRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        try:
            return operator_service.switch_mode(payload.actor, payload.trading_mode, payload.confirmation_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/manual/positions")
    async def manual_positions(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return manual_trade_service.list_positions("dashboard", "dashboard")

    @router.post("/api/manual/open")
    async def manual_open(payload: ManualTradeOpenRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.open_market_trade(
            actor=payload.actor,
            source="dashboard",
            symbol=payload.symbol,
            side=payload.side,
            volume=payload.volume,
            sl=payload.sl,
            tp=payload.tp,
            comment=payload.comment,
        )

    @router.post("/api/manual/close")
    async def manual_close(payload: PositionTicketRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.close_position(payload.actor, "dashboard", payload.ticket)

    @router.post("/api/manual/partial-close")
    async def manual_partial_close(payload: PartialCloseRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.partial_close(payload.actor, "dashboard", payload.ticket, payload.volume)

    @router.post("/api/manual/modify")
    async def manual_modify(payload: ModifyPositionRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.modify_position(payload.actor, "dashboard", payload.ticket, payload.sl, payload.tp)

    @router.post("/api/manual/breakeven")
    async def manual_breakeven(payload: PositionTicketRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.move_to_break_even(payload.actor, "dashboard", payload.ticket)

    @router.post("/api/manual/close-all")
    async def manual_close_all(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.close_all_positions(payload.actor, "dashboard")

    @router.post("/api/control/close-all")
    async def control_close_all(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.close_all_positions(payload.actor, "dashboard")

    @router.post("/api/jobs/backtest")
    async def create_backtest_job(payload: JobBacktestRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        request_payload = {
            "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            "enabled_strategies": payload.enabled_strategies,
            "session_filter": payload.session_filter,
            "initial_balance": payload.initial_balance,
            "risk_percent": payload.risk_percent,
            "spread_model": payload.spread_model,
            "slippage_model": payload.slippage_model,
            "execution_model": payload.execution_model,
            "engine_mode": payload.engine_mode,
            "notes": payload.notes,
        }
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        result = job_service.create_backtest_job(request_payload, actor=payload.actor)
        audit_service.log_action(
            payload.actor,
            "backtest_job_created",
            payload.symbol,
            {
                "timeframe": payload.timeframe,
                "start_date": payload.start_date,
                "end_date": payload.end_date or payload.start_date,
                "enabled_strategies": payload.enabled_strategies,
                "job_id": result.get("id"),
            },
        )
        warning = result.get("warning") or result.get("metadata_json", {}).get("warning")
        return {"ok": True, "job": result, "worker": result.get("metadata_json", {}).get("worker_status"), "warning": warning}

    @router.post("/api/jobs/oos")
    async def create_oos_job(payload: JobOOSRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        request_payload = {
            "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            "enabled_strategies": payload.enabled_strategies,
            "session_filter": {"enabled": bool(payload.allowed_sessions), "allowed_sessions": payload.allowed_sessions},
            "initial_balance": payload.initial_balance,
            "risk_percent": payload.risk_percent,
            "spread_model": {"type": "fixed_points", "points": payload.spread_points},
            "slippage_model": {"type": "fixed_points", "points": payload.slippage_points},
            "execution_model": payload.execution_model,
            "engine_mode": payload.engine_mode,
            "notes": payload.notes,
        }
        result = job_service.create_oos_job(
            request=request_payload,
            actor=payload.actor,
            baseline_run_id=payload.baseline_run_id,
            baseline_bundle_path=payload.baseline_bundle_path,
            baseline_database_path=payload.baseline_database_path,
            output_dir=payload.output_dir,
            title=payload.title,
            notes=payload.notes,
        )
        audit_service.log_action(payload.actor, "oos_job_created", payload.symbol, {"request": request_payload, "job_id": result.get("id")})
        return {"ok": True, "job": result}

    @router.get("/api/jobs/{job_id}")
    async def get_job(job_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        try:
            return {"ok": True, "job": job_service.get_job(job_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs")
    async def list_jobs(
        limit: int = 30,
        job_type: str | None = None,
        state: str | None = None,
        _: None = Depends(dashboard_auth.dependency),
    ) -> dict[str, Any]:
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        return {"ok": True, "jobs": job_service.list_jobs(limit=limit, job_type=job_type, state=state)}

    @router.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: int, payload: JobResumeRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        if int(payload.job_id) != int(job_id):
            raise HTTPException(status_code=400, detail="job_id mismatch")
        try:
            job = job_service.resume_job(job_id, actor=payload.actor)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "job_resume", str(job_id), {"new_job_id": job.get("id")})
        return {"ok": True, "job": job}

    @router.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int, payload: JobCancelRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if job_service is None:
            raise HTTPException(status_code=503, detail="Job service not configured")
        if int(payload.job_id) != int(job_id):
            raise HTTPException(status_code=400, detail="job_id mismatch")
        try:
            job = job_service.cancel_job(job_id, actor=payload.actor)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "job_cancel", str(job_id), {"state": job.get("state")})
        return {"ok": True, "job": job}

    @router.post("/api/backtest/run")
    async def run_backtest(payload: BacktestRunRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        result = await create_backtest_job(JobBacktestRequest(**payload.model_dump()), _)
        return {"ok": True, "job": result.get("job"), "result": None}

    @router.post("/api/oos/run")
    async def run_oos(payload: OOSRunRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if job_service is None:
            if oos_service is None:
                raise HTTPException(status_code=503, detail="OOS service not configured")
            request_payload = {
                "symbol": payload.symbol,
                "timeframe": payload.timeframe,
                "start_date": payload.start_date,
                "end_date": payload.end_date,
                "enabled_strategies": payload.enabled_strategies,
                "session_filter": {"enabled": bool(payload.allowed_sessions), "allowed_sessions": payload.allowed_sessions},
                "initial_balance": payload.initial_balance,
                "risk_percent": payload.risk_percent,
                "spread_model": {"type": "fixed_points", "points": payload.spread_points},
                "slippage_model": {"type": "fixed_points", "points": payload.slippage_points},
                "execution_model": payload.execution_model,
                "engine_mode": payload.engine_mode,
                "notes": payload.notes,
            }
            result = oos_service.start_run(
                request=request_payload,
                actor=payload.actor,
                baseline_run_id=payload.baseline_run_id,
                baseline_bundle_path=payload.baseline_bundle_path,
                baseline_database_path=payload.baseline_database_path,
                output_dir=payload.output_dir,
                title=payload.title,
                notes=payload.notes,
            )
            audit_service.log_action(payload.actor, "oos_run_created", payload.symbol, {"request": request_payload})
            return {"ok": True, "job": None, "run": result.get("run"), "state": result.get("state")}
        result = await create_oos_job(JobOOSRequest(**payload.model_dump()), _)
        return {"ok": True, "job": result.get("job")}

    @router.get("/api/oos/status")
    async def get_oos_status(oos_run_id: int | None = None, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if oos_service is None:
            raise HTTPException(status_code=503, detail="OOS service not configured")
        payload = oos_service.get_status(oos_run_id)
        payload.setdefault("history", [])
        payload.setdefault("active_run_id", None)
        payload.setdefault("state", "IDLE")
        return payload

    @router.post("/api/oos/resume")
    async def resume_oos(payload: OOSResumeRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if job_service is None:
            if oos_service is None:
                raise HTTPException(status_code=503, detail="OOS service not configured")
            try:
                result = oos_service.resume_run(payload.oos_run_id, rerun_partial=payload.rerun_partial)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            audit_service.log_action(payload.actor, "oos_resume", str(payload.oos_run_id), {"rerun_partial": payload.rerun_partial})
            return {"ok": True, "job": None, "run": result.get("run"), "state": result.get("state")}
        try:
            result = job_service.create_oos_job(
                request={},
                actor=payload.actor,
                rerun_partial=payload.rerun_partial,
                existing_oos_run_id=payload.oos_run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "oos_resume", str(payload.oos_run_id), {"rerun_partial": payload.rerun_partial, "job_id": result.get("id")})
        return {"ok": True, "job": result}

    @router.post("/api/oos/rebuild-report")
    async def rebuild_oos_report(payload: OOSRebuildRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if oos_service is None:
            raise HTTPException(status_code=503, detail="OOS service not configured")
        try:
            result = oos_service.rebuild_report(payload.oos_run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "oos_rebuild_report", str(payload.oos_run_id), result)
        return result

    @router.get("/api/oos/history")
    async def list_oos_history(limit: int = 20, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if oos_service is None:
            raise HTTPException(status_code=503, detail="OOS service not configured")
        payload = oos_service.history(limit=limit)
        payload.setdefault("runs", [])
        return payload

    @router.get("/api/backtest/runs")
    async def list_backtest_runs(limit: int = 20, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "runs": dashboard_data.list_backtest_runs(limit=limit)}

    @router.get("/api/backtest/run/{run_id}")
    async def get_backtest_run(run_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "result": dashboard_data.get_backtest_run_bundle(run_id)}

    @router.get("/api/backtests/{run_id}/replay/summary")
    async def get_backtest_replay_summary(run_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "result": dashboard_data.get_backtest_replay_summary(run_id)}

    @router.get("/api/backtests/{run_id}/replay/window")
    async def get_backtest_replay_window(
        run_id: int,
        start_bar: int = 0,
        end_bar: int = -1,
        _: None = Depends(dashboard_auth.dependency),
    ) -> dict[str, Any]:
        if end_bar < 0:
            raise HTTPException(status_code=400, detail="end_bar is required")
        return {"ok": True, "result": dashboard_data.get_backtest_replay_window(run_id, start_bar, end_bar)}

    @router.get("/api/backtests/{run_id}/replay/events")
    async def get_backtest_replay_events(
        run_id: int,
        start_bar: int = 0,
        end_bar: int = -1,
        _: None = Depends(dashboard_auth.dependency),
    ) -> dict[str, Any]:
        if end_bar < 0:
            raise HTTPException(status_code=400, detail="end_bar is required")
        return {"ok": True, "result": dashboard_data.get_backtest_replay_events(run_id, start_bar, end_bar)}

    @router.get("/api/backtests/{run_id}/replay/frame/{bar_index}")
    async def get_backtest_replay_frame(run_id: int, bar_index: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        frame = dashboard_data.get_backtest_replay_frame(run_id, bar_index)
        if frame is None:
            raise HTTPException(status_code=404, detail="Playback frame not found")
        return {"ok": True, "result": frame}

    @router.get("/api/backtests/{run_id}/stream/status")
    async def get_backtest_stream_status(run_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        try:
            return {"ok": True, "result": dashboard_data.get_backtest_stream_status(run_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/backtests/{run_id}/stream/window")
    async def get_backtest_stream_window(
        run_id: int,
        after_bar: int = -1,
        _: None = Depends(dashboard_auth.dependency),
    ) -> dict[str, Any]:
        if after_bar < 0:
            raise HTTPException(status_code=400, detail="after_bar is required")
        return {"ok": True, "result": dashboard_data.get_backtest_stream_window(run_id, after_bar)}

    return router
