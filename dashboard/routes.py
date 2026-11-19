"""FastAPI routes for the professional trading bot dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from dashboard.schemas import (
    ConfigPatchRequest,
    ExecutionModeRequest,
    IndicatorCampaignRequest,
    IndicatorDatasetCreateRequest,
    JobCancelRequest,
    JobResumeRequest,
    ModeSwitchRequest,
    PresetRequest,
    RawConfigImportRequest,
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
    indicator_lab_service = services.get("indicator_lab_service")
    lebprime_live_service = services.get("lebprime_live_service")
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

    @router.get("/api/strategies/lebprime-v3-4-0/status")
    async def get_lebprime_v3_4_0_status(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if lebprime_live_service is None:
            return {"ok": False, "strategy": "lebprime_v3_4_0", "available": False}
        lebprime_live_service.refresh_config(config_manager.dashboard_view())
        return {"ok": True, "available": True, **lebprime_live_service.status_snapshot()}

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

    @router.get("/api/lebprime/trades")
    async def lebprime_trades(days: int = 30, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        try:
            return manual_trade_service.trade_history(days)
        except Exception as exc:  # MT5 unavailable: show the message instead of failing the page
            return {"ok": False, "message": str(exc), "trades": [], "summary": {}}

    @router.post("/api/control/close-all")
    async def control_close_all(payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        return manual_trade_service.close_all_positions(payload.actor, "dashboard")

    @router.get("/api/indicator-lab/datasets")
    async def indicator_lab_datasets(_: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        return {"ok": True, "datasets": indicator_lab_service.dataset_service.list_datasets()}

    @router.post("/api/indicator-lab/datasets/mt5")
    async def indicator_lab_create_dataset(payload: IndicatorDatasetCreateRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        try:
            dataset = indicator_lab_service.dataset_service.create_mt5_dataset(
                name=payload.name,
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                start_date=payload.start_date,
                end_date=payload.end_date,
                notes=payload.notes,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "indicator_dataset_created", payload.name, {"dataset_id": dataset.get("id"), "symbol": payload.symbol, "timeframe": payload.timeframe})
        return {"ok": True, "dataset": dataset}

    @router.get("/api/indicator-lab/datasets/{dataset_id}")
    async def indicator_lab_get_dataset(dataset_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        dataset = indicator_lab_service.dataset_service.get_dataset(dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return {"ok": True, "dataset": dataset, "hash": indicator_lab_service.dataset_service.verify_hash(dataset_id)}

    @router.delete("/api/indicator-lab/datasets/{dataset_id}")
    async def indicator_lab_delete_dataset(dataset_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        try:
            indicator_lab_service.dataset_service.delete_dataset(dataset_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @router.get("/api/indicator-lab/parameters/{indicator}")
    async def indicator_lab_parameters(indicator: str, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        try:
            return {"ok": True, **indicator_lab_service.parameter_schema(indicator), "pine_reference_hash": indicator_lab_service.pine_hash(indicator)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/indicator-lab/campaigns/preview")
    async def indicator_lab_preview_campaign(payload: IndicatorCampaignRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        try:
            preview = indicator_lab_service.preview_campaign(dataset_id=payload.dataset_id, indicator=payload.indicator, fixed_parameters=payload.fixed_parameters, optimization_grid=payload.optimization_grid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "preview": preview}

    @router.post("/api/indicator-lab/campaigns")
    async def indicator_lab_create_campaign(payload: IndicatorCampaignRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        try:
            result = indicator_lab_service.create_campaign(
                dataset_id=payload.dataset_id,
                indicator=payload.indicator,
                fixed_parameters=payload.fixed_parameters,
                optimization_grid=payload.optimization_grid,
                name=payload.name,
                notes=payload.notes,
                confirm_large=payload.confirm_large,
                actor=payload.actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_service.log_action(payload.actor, "indicator_campaign_created", payload.indicator, {"campaign_id": (result.get("campaign") or {}).get("id"), "job_id": result.get("job_id")})
        return {"ok": True, **result}

    @router.get("/api/indicator-lab/campaigns")
    async def indicator_lab_campaigns(limit: int = 30, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        return {"ok": True, "campaigns": indicator_lab_service.list_campaigns(limit=limit)}

    @router.get("/api/indicator-lab/campaigns/{campaign_id}")
    async def indicator_lab_get_campaign(campaign_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        if indicator_lab_service is None:
            raise HTTPException(status_code=503, detail="Indicator Lab service not configured")
        campaign = indicator_lab_service.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return {"ok": True, "campaign": campaign}

    @router.post("/api/indicator-lab/campaigns/{campaign_id}/pause")
    async def indicator_lab_pause_campaign(campaign_id: int, payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        campaign = indicator_lab_service.set_campaign_state(campaign_id, "PAUSED")
        return {"ok": True, "campaign": campaign}

    @router.post("/api/indicator-lab/campaigns/{campaign_id}/resume")
    async def indicator_lab_resume_campaign(campaign_id: int, payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        campaign = indicator_lab_service.set_campaign_state(campaign_id, "QUEUED")
        job_id = database.create_job({"job_type": "INDICATOR_CAMPAIGN", "state": "QUEUED", "payload_json": {"campaign_id": campaign_id, "actor": payload.actor}, "related_run_type": "indicator_campaign", "related_run_id": campaign_id})
        return {"ok": True, "campaign": campaign, "job_id": job_id}

    @router.post("/api/indicator-lab/campaigns/{campaign_id}/cancel")
    async def indicator_lab_cancel_campaign(campaign_id: int, payload: ToggleRequest, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        assert_not_readonly()
        campaign = indicator_lab_service.set_campaign_state(campaign_id, "CANCELED")
        return {"ok": True, "campaign": campaign}

    @router.get("/api/indicator-lab/campaigns/{campaign_id}/results")
    async def indicator_lab_campaign_results(campaign_id: int, limit: int = 500, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "results": indicator_lab_service.campaign_results(campaign_id, limit=limit)}

    @router.get("/api/indicator-lab/campaigns/{campaign_id}/results/export.csv")
    async def indicator_lab_campaign_results_csv(campaign_id: int, _: None = Depends(dashboard_auth.dependency)) -> Response:
        campaign = indicator_lab_service.get_campaign(campaign_id)
        indicator = (campaign or {}).get("indicator") or "indicator"
        filename = f"{indicator}_campaign_{campaign_id}_results.csv"
        return Response(
            indicator_lab_service.campaign_results_csv(campaign_id),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/indicator-lab/tests/{test_id}")
    async def indicator_lab_get_test(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        test = indicator_lab_service.get_test(test_id)
        if not test:
            raise HTTPException(status_code=404, detail="Test not found")
        return {"ok": True, "test": test}

    @router.get("/api/indicator-lab/tests/{test_id}/zones")
    async def indicator_lab_test_zones(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "zones": indicator_lab_service.test_zones(test_id)}

    @router.get("/api/indicator-lab/tests/{test_id}/zones/export.csv")
    async def indicator_lab_test_zones_csv(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> Response:
        test = indicator_lab_service.get_test(test_id) or {}
        filename = f"{test.get('indicator', 'indicator')}_test_{test_id}_zones.csv"
        return Response(
            indicator_lab_service.zones_csv(test_id),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/indicator-lab/tests/{test_id}/candidates/export.csv")
    async def indicator_lab_test_candidates_csv(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> Response:
        test = indicator_lab_service.get_test(test_id) or {}
        filename = f"{test.get('indicator', 'indicator')}_test_{test_id}_candidates.csv"
        return Response(
            indicator_lab_service.candidates_csv(test_id),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/indicator-lab/zones/{zone_id}")
    async def indicator_lab_zone_detail(zone_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        zone = indicator_lab_service.get_zone(zone_id)
        if not zone:
            raise HTTPException(status_code=404, detail="Zone not found")
        return {"ok": True, "zone": zone}

    @router.get("/api/indicator-lab/zones/{zone_id}/events")
    async def indicator_lab_zone_events(zone_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "events": indicator_lab_service.zone_events(zone_id)}

    @router.get("/api/indicator-lab/tests/{test_id}/events")
    async def indicator_lab_test_events(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        return {"ok": True, "events": indicator_lab_service.test_events(test_id)}

    @router.get("/api/indicator-lab/tests/{test_id}/events/export.csv")
    async def indicator_lab_test_events_csv(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> Response:
        test = indicator_lab_service.get_test(test_id) or {}
        filename = f"{test.get('indicator', 'indicator')}_test_{test_id}_events.csv"
        return Response(
            indicator_lab_service.events_csv(test_id),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/api/indicator-lab/tests/{test_id}/replay")
    async def indicator_lab_test_replay(test_id: int, _: None = Depends(dashboard_auth.dependency)) -> dict[str, Any]:
        try:
            return {"ok": True, "result": indicator_lab_service.test_replay(test_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    return router
