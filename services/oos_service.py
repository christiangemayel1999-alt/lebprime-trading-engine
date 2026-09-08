"""Background OOS orchestration, persistence, resume, and report rebuild helpers."""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from services.backtest_storage import BacktestStorage
from services.config_manager import ConfigManager
from services.database import DatabaseService
from trading_bot.backtest.oos_evaluation import OOSEvaluationFramework, ScenarioResult, ScenarioSpec
from trading_bot.core.job_state import JobState, is_active_job_state, normalize_job_state
from utils import ensure_directory, to_utc, utc_now


class OOSService:
    """Manage durable OOS matrix jobs for the dashboard and recovery flows."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        database: DatabaseService,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.database = database
        self._storage = BacktestStorage(database)
        self._runtime_logger = logging.getLogger("mtf_sniper_bot")
        self._recover_legacy_runs()
        self._reconcile_stale_runs()

    @staticmethod
    def _parse_json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return deepcopy(default)
        if isinstance(value, (dict, list)):
            return deepcopy(value)
        try:
            import json

            parsed = json.loads(value)
            return parsed if parsed is not None else deepcopy(default)
        except Exception:
            return deepcopy(default)

    def _framework(self) -> OOSEvaluationFramework:
        config = self.config_manager.dashboard_view()
        db_path = self.base_dir / config["storage"]["database_path"]
        return OOSEvaluationFramework(self.base_dir, config, database_path=db_path)

    def _scenario_specs_from_run(self, row: dict[str, Any]) -> list[ScenarioSpec]:
        return self._framework().default_scenarios(
            baseline_run_id=row.get("baseline_run_id"),
            baseline_bundle_path=row.get("baseline_bundle_path"),
        )

    def _default_output_dir(self, oos_run_id: int) -> str:
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        return str(ensure_directory(self.base_dir / "reports" / f"oos_eval_{oos_run_id:04d}_{stamp}"))

    def _log(self, event_type: str, payload: dict[str, Any]) -> None:
        self._runtime_logger.info("%s %s", event_type, payload)

    def _recover_legacy_runs(self) -> None:
        """Build OOS manifest rows from older scenario-tagged backtest runs."""
        if self.database.get_oos_runs(limit=1):
            return
        scenario_map = {spec.key: spec for spec in self._framework().default_scenarios()}
        backtest_runs = self.database.get_backtest_runs(limit=250)
        groups: dict[tuple[str, str, str, str], dict[str, dict[str, Any]]] = {}
        for row in backtest_runs:
            notes = str(row.get("notes") or "")
            if "|" not in notes:
                continue
            scenario_key, _, scenario_label = notes.partition("|")
            if scenario_key not in scenario_map:
                continue
            group_key = (
                str(row.get("symbol") or ""),
                str(row.get("timeframe") or ""),
                str(row.get("start_date") or ""),
                str(row.get("end_date") or ""),
            )
            current = groups.setdefault(group_key, {})
            existing = current.get(scenario_key)
            if existing is None or int(row.get("id") or 0) > int(existing.get("id") or 0):
                current[scenario_key] = {
                    **row,
                    "recovered_scenario_label": scenario_label or scenario_map[scenario_key].label,
                }
        for (symbol, timeframe, start_date, end_date), recovered in groups.items():
            if not recovered:
                continue
            first_row = sorted(recovered.values(), key=lambda item: int(item.get("id") or 0))[0]
            created_at = str(first_row.get("created_at") or utc_now().isoformat())
            oos_run_id = self.database.create_oos_run(
                {
                    "created_at": created_at,
                    "updated_at": utc_now().isoformat(),
                    "finished_at": utc_now().isoformat(),
                    "state": JobState.COMPLETED.value if len(recovered) == len(scenario_map) else JobState.PARTIAL.value,
                    "title": "Recovered Legacy OOS Matrix",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start_date": start_date,
                    "end_date": end_date,
                    "request_json": {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "start_date": start_date[:10],
                        "end_date": end_date[:10],
                        "enabled_strategies": self._parse_json(first_row.get("enabled_strategies_json"), []),
                        "session_filter": self._parse_json(first_row.get("session_filter_json"), {}),
                        "initial_balance": float(first_row.get("initial_balance", 0.0) or 0.0),
                        "risk_percent": float(first_row.get("risk_percent", 0.0) or 0.0),
                        "spread_model": self._parse_json(first_row.get("spread_model"), {}),
                        "slippage_model": self._parse_json(first_row.get("slippage_model"), {}),
                        "execution_model": str(first_row.get("execution_model") or "next_bar_open"),
                        "notes": "recovered_from_legacy_backtest_runs",
                    },
                    "progress_current": len(recovered),
                    "progress_total": len(scenario_map),
                    "notes": "legacy_recovered",
                    "metadata_json": {"recovered_from_backtest_runs": sorted(int(item.get("id") or 0) for item in recovered.values())},
                }
            )
            for index, spec in enumerate(scenario_map.values(), start=1):
                row = recovered.get(spec.key)
                status = JobState.COMPLETED.value if row else JobState.INTERRUPTED.value
                self.database.upsert_oos_scenario(
                    {
                        "oos_run_id": oos_run_id,
                        "scenario_index": index,
                        "scenario_key": spec.key,
                        "scenario_label": spec.label,
                        "source": spec.source,
                        "status": status,
                        "created_at": created_at,
                        "updated_at": utc_now().isoformat(),
                        "finished_at": utc_now().isoformat() if row else None,
                        "run_id": row.get("id") if row else None,
                        "warning_json": [],
                        "error": None if row else "legacy_scenario_missing",
                        "metadata_json": {"description": spec.description, "legacy_recovered": bool(row)},
                    }
                )
            self._log(
                "oos_legacy_recovered",
                {
                    "oos_run_id": oos_run_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start_date": start_date,
                    "end_date": end_date,
                    "scenario_count": len(recovered),
                },
            )

    def _reconcile_stale_runs(self, stale_seconds: int = 300) -> None:
        """Convert heartbeat-stale active OOS runs into terminal interrupted/partial states."""
        for row in self.database.get_active_oos_runs():
            run_id = int(row["id"])
            if normalize_job_state(row.get("state")) != JobState.RUNNING.value:
                continue
            heartbeat_at = to_utc(row.get("last_heartbeat_at"))
            if heartbeat_at is None:
                continue
            if (utc_now() - heartbeat_at).total_seconds() <= float(stale_seconds):
                continue
            scenarios = self.database.get_oos_scenarios(run_id)
            completed = sum(1 for item in scenarios if normalize_job_state(item.get("status")) == JobState.COMPLETED.value)
            total = len(scenarios)
            final_state = JobState.PARTIAL.value if completed else JobState.INTERRUPTED.value
            reason = "oos_heartbeat_stale"
            self.database.update_oos_run(
                run_id,
                {
                    "state": final_state,
                    "finished_at": utc_now().isoformat(),
                    "interrupted_reason": reason,
                    "progress_current": completed,
                    "progress_total": total,
                },
            )
            for scenario in scenarios:
                if normalize_job_state(scenario.get("status")) in {JobState.QUEUED.value, JobState.RUNNING.value}:
                    self.database.update_oos_scenario(
                        int(scenario["id"]),
                        {
                            "status": JobState.INTERRUPTED.value,
                            "finished_at": utc_now().isoformat(),
                            "error": reason,
                        },
                    )

    def _scenario_status_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "status": normalize_job_state(row.get("status")),
            "warning_json": self._parse_json(row.get("warning_json"), []),
            "metadata_json": self._parse_json(row.get("metadata_json"), {}),
        }

    def _run_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        scenarios = [self._scenario_status_payload(item) for item in self.database.get_oos_scenarios(int(row["id"]))]
        progress_total = int(row.get("progress_total") or len(scenarios) or 0)
        progress_current = int(row.get("progress_current") or sum(1 for item in scenarios if item["status"] == JobState.COMPLETED.value))
        current_scenario = next((item for item in scenarios if item["status"] == JobState.RUNNING.value), None)
        completed_count = sum(1 for item in scenarios if item["status"] == JobState.COMPLETED.value)
        failed_count = sum(1 for item in scenarios if item["status"] == JobState.FAILED.value)
        interrupted_count = sum(1 for item in scenarios if item["status"] == JobState.INTERRUPTED.value)
        payload = {
            **row,
            "state": normalize_job_state(row.get("state")),
            "request_json": self._parse_json(row.get("request_json"), {}),
            "metadata_json": self._parse_json(row.get("metadata_json"), {}),
            "progress": {
                "current": progress_current,
                "total": progress_total,
                "pct": round((progress_current / progress_total) * 100.0, 2) if progress_total else 0.0,
                "completed_scenarios": completed_count,
                "failed_scenarios": failed_count,
                "interrupted_scenarios": interrupted_count,
            },
            "current_scenario": current_scenario,
            "scenarios": scenarios,
            "active": is_active_job_state(row.get("state")),
        }
        return payload

    def start_run(
        self,
        *,
        request: dict[str, Any],
        actor: str = "dashboard",
        baseline_run_id: int | None = None,
        baseline_bundle_path: str | None = None,
        baseline_database_path: str | None = None,
        output_dir: str | None = None,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Create a new OOS matrix manifest row without executing heavy work."""
        framework = self._framework()
        scenarios = framework.default_scenarios(
            baseline_run_id=baseline_run_id,
            baseline_bundle_path=baseline_bundle_path,
        )
        created_at = utc_now().isoformat()
        oos_run_id = self.database.create_oos_run(
            {
                "created_at": created_at,
                "updated_at": created_at,
                "state": JobState.QUEUED.value,
                "title": title or "OOS Matrix",
                "symbol": request["symbol"],
                "timeframe": request["timeframe"],
                "start_date": request["start_date"],
                "end_date": request["end_date"],
                "request_json": request,
                "baseline_run_id": baseline_run_id,
                "baseline_bundle_path": baseline_bundle_path,
                "baseline_database_path": baseline_database_path,
                "output_dir": output_dir,
                "progress_current": 0,
                "progress_total": len(scenarios),
                "notes": notes,
                "metadata_json": {
                    "actor": actor,
                    "requested_execution_mode": str(request.get("engine_mode") or ""),
                },
            }
        )
        resolved_output_dir = output_dir or self._default_output_dir(oos_run_id)
        self.database.update_oos_run(oos_run_id, {"output_dir": resolved_output_dir})
        for index, spec in enumerate(scenarios, start=1):
            self.database.upsert_oos_scenario(
                {
                    "oos_run_id": oos_run_id,
                    "scenario_index": index,
                    "scenario_key": spec.key,
                    "scenario_label": spec.label,
                    "source": spec.source,
                    "status": JobState.QUEUED.value,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "warning_json": [],
                    "metadata_json": {"description": spec.description, **deepcopy(spec.metadata)},
                }
            )
        self._log(
            "oos_run_requested",
            {
                "oos_run_id": oos_run_id,
                "actor": actor,
                "request": request,
                "baseline_run_id": baseline_run_id,
                "baseline_bundle_path": baseline_bundle_path,
                "baseline_database_path": baseline_database_path,
                "output_dir": resolved_output_dir,
                "requested_execution_mode": str(request.get("engine_mode") or ""),
            },
        )
        return self.get_status(oos_run_id)

    def resume_run(self, oos_run_id: int, *, rerun_partial: bool = False) -> dict[str, Any]:
        """Reset an interrupted or partial OOS matrix so a worker can resume it."""
        self._reconcile_stale_runs()
        row = self.database.get_oos_run(oos_run_id)
        if not row:
            raise ValueError(f"OOS run {oos_run_id} not found")
        scenario_updates = self.database.get_oos_scenarios(oos_run_id)
        for scenario in scenario_updates:
            status = normalize_job_state(scenario.get("status"))
            if status == JobState.COMPLETED.value:
                continue
            if not rerun_partial and status == JobState.FAILED.value:
                continue
            self.database.update_oos_scenario(
                int(scenario["id"]),
                {
                    "status": JobState.QUEUED.value,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                },
            )
        self.database.update_oos_run(
            oos_run_id,
            {
                "state": JobState.QUEUED.value,
                "started_at": None,
                "finished_at": None,
                "interrupted_reason": None,
                "failure_reason": None,
                "report_error": None,
                "report_status": "pending",
            },
        )
        return self.get_status(oos_run_id)

    def _load_completed_result(
        self,
        *,
        framework: OOSEvaluationFramework,
        spec: ScenarioSpec,
        scenario_row: dict[str, Any],
        oos_row: dict[str, Any],
    ) -> ScenarioResult | None:
        """Rehydrate one completed scenario from existing bundle/run artifacts."""
        status = normalize_job_state(scenario_row.get("status"))
        if status != JobState.COMPLETED.value:
            return None
        warnings = self._parse_json(scenario_row.get("warning_json"), [])
        metadata = self._parse_json(scenario_row.get("metadata_json"), {})
        if spec.source == "external_bundle":
            bundle_path = scenario_row.get("bundle_path") or oos_row.get("baseline_bundle_path")
            if not bundle_path:
                return None
            bundle = framework._load_bundle_from_path(Path(str(bundle_path)))
        else:
            run_id = scenario_row.get("run_id")
            if run_id in (None, ""):
                return None
            bundle = self._storage.get_run_bundle(int(run_id))
            artifacts = bundle.get("artifacts") or bundle.get("run", {}).get("artifacts_json") or {}
            if not bundle.get("artifacts") and isinstance(artifacts, dict) and artifacts:
                bundle["artifacts"] = artifacts
        return ScenarioResult(
            key=spec.key,
            label=spec.label,
            source=spec.source,
            bundle=bundle,
            description=spec.description,
            warnings=list(warnings),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _store_scenario_result(
        self,
        *,
        oos_run_id: int,
        spec: ScenarioSpec,
        result: ScenarioResult,
        error: str | None = None,
    ) -> None:
        bundle = result.bundle if isinstance(result.bundle, dict) else {}
        run_payload = bundle.get("run", {}) if isinstance(bundle.get("run"), dict) else {}
        artifacts = bundle.get("artifacts") if isinstance(bundle.get("artifacts"), dict) else {}
        self.database.upsert_oos_scenario(
            {
                "oos_run_id": oos_run_id,
                "scenario_index": next(
                    (idx for idx, candidate in enumerate(self._scenario_specs_from_run(self.database.get_oos_run(oos_run_id) or {}), start=1) if candidate.key == spec.key),
                    0,
                ),
                "scenario_key": spec.key,
                "scenario_label": spec.label,
                "source": spec.source,
                "status": JobState.COMPLETED.value if error is None else JobState.FAILED.value,
                "started_at": utc_now().isoformat(),
                "finished_at": utc_now().isoformat(),
                "run_id": run_payload.get("id"),
                "export_path": artifacts.get("run_dir"),
                "bundle_path": artifacts.get("bundle_json") or self.database.get_oos_run(oos_run_id).get("baseline_bundle_path"),
                "report_path": artifacts.get("summary"),
                "warning_json": list(result.warnings),
                "error": error,
                "metadata_json": deepcopy(result.metadata),
            }
        )

    def _report_paths(self, output_dir: str | Path) -> dict[str, str]:
        path = Path(output_dir)
        return {
            "output_dir": str(path),
            "report_path": str(path / "report.json"),
            "summary_path": str(path / "evaluation_summary.md"),
            "methodology_path": str(path / "comparison_methodology.md"),
            "charts_path": str(path / "recommended_charts.md"),
        }

    def rebuild_report(self, oos_run_id: int) -> dict[str, Any]:
        """Rebuild final OOS artifacts from completed scenario rows."""
        row = self.database.get_oos_run(oos_run_id)
        if not row:
            raise ValueError(f"OOS run {oos_run_id} not found")
        framework = self._framework()
        specs = self._scenario_specs_from_run(row)
        scenario_rows = {item["scenario_key"]: item for item in self.database.get_oos_scenarios(oos_run_id)}
        results: list[ScenarioResult] = []
        for spec in specs:
            scenario_row = scenario_rows.get(spec.key)
            if not scenario_row:
                continue
            loaded = self._load_completed_result(
                framework=framework,
                spec=spec,
                scenario_row=scenario_row,
                oos_row=row,
            )
            if loaded is not None:
                results.append(loaded)
        if not results:
            raise ValueError(f"OOS run {oos_run_id} has no completed scenarios to rebuild")
        output_dir = row.get("output_dir") or self._default_output_dir(oos_run_id)
        ensure_directory(output_dir)
        request = self._parse_json(row.get("request_json"), {})
        self._log("oos_report_rebuild_started", {"oos_run_id": oos_run_id, "completed_scenarios": len(results), "output_dir": output_dir})
        report = framework.build_report(request, results)
        framework.write_report(output_dir, report)
        paths = self._report_paths(output_dir)
        self.database.update_oos_run(
            oos_run_id,
            {
                "output_dir": output_dir,
                "report_status": "completed",
                "report_path": paths["report_path"],
                "report_error": None,
            },
        )
        self._log("oos_report_rebuild_completed", {"oos_run_id": oos_run_id, **paths})
        return {"ok": True, **paths}

    def execute_run(
        self,
        oos_run_id: int,
        *,
        rerun_partial: bool,
        progress_callback: Any | None = None,
        should_interrupt: Any | None = None,
    ) -> dict[str, Any]:
        """Execute one OOS matrix synchronously for the external worker."""
        completed_results: list[ScenarioResult] = []
        failed = False
        try:
            row = self.database.get_oos_run(oos_run_id)
            if not row:
                raise ValueError(f"OOS run {oos_run_id} not found")
            framework = self._framework()
            specs = self._scenario_specs_from_run(row)
            output_dir = row.get("output_dir") or self._default_output_dir(oos_run_id)
            ensure_directory(output_dir)
            request = self._parse_json(row.get("request_json"), {})
            self.database.update_oos_run(
                oos_run_id,
                {
                    "state": JobState.RUNNING.value,
                    "started_at": row.get("started_at") or utc_now().isoformat(),
                    "updated_at": utc_now().isoformat(),
                    "last_heartbeat_at": utc_now().isoformat(),
                    "progress_total": len(specs),
                    "output_dir": output_dir,
                    "report_status": "pending",
                },
            )
            self._log("oos_run_started", {"oos_run_id": oos_run_id, "request": request, "output_dir": output_dir})
            scenario_rows = {item["scenario_key"]: item for item in self.database.get_oos_scenarios(oos_run_id)}
            for index, spec in enumerate(specs, start=1):
                if callable(should_interrupt) and bool(should_interrupt()):
                    raise InterruptedError("oos_job_cancel_requested")
                scenario_row = scenario_rows.get(spec.key)
                if scenario_row and normalize_job_state(scenario_row.get("status")) == JobState.COMPLETED.value:
                    loaded = self._load_completed_result(
                        framework=framework,
                        spec=spec,
                        scenario_row=scenario_row,
                        oos_row=row,
                    )
                    if loaded is not None:
                        completed_results.append(loaded)
                        self.database.update_oos_run(
                            oos_run_id,
                            {
                                "progress_current": len(completed_results),
                                "last_heartbeat_at": utc_now().isoformat(),
                            },
                        )
                        if callable(progress_callback):
                            progress_callback(oos_run_id, len(completed_results), len(specs), spec.key)
                        continue
                if scenario_row and not rerun_partial and normalize_job_state(scenario_row.get("status")) in {JobState.FAILED.value, JobState.PARTIAL.value}:
                    failed = True
                    continue
                self.database.upsert_oos_scenario(
                    {
                        "oos_run_id": oos_run_id,
                        "scenario_index": index,
                        "scenario_key": spec.key,
                        "scenario_label": spec.label,
                        "source": spec.source,
                        "status": JobState.RUNNING.value,
                        "started_at": utc_now().isoformat(),
                        "updated_at": utc_now().isoformat(),
                        "error": None,
                    }
                )
                self.database.update_oos_run(
                    oos_run_id,
                    {
                        "progress_current": len(completed_results),
                        "last_heartbeat_at": utc_now().isoformat(),
                    },
                )
                self._log(
                    "oos_scenario_mode_resolved",
                    {
                        "oos_run_id": oos_run_id,
                        "scenario_key": spec.key,
                        "scenario_label": spec.label,
                        "requested_execution_mode": spec.execution_mode,
                        "scenario_overrides": deepcopy(spec.overrides),
                    },
                )
                try:
                    result = framework.materialize_scenario(
                        spec=spec,
                        request=request,
                        baseline_database_path=row.get("baseline_database_path"),
                    )
                    bundle = result.bundle if isinstance(result.bundle, dict) else {}
                    run_payload = bundle.get("run", {}) if isinstance(bundle.get("run"), dict) else {}
                    artifacts = bundle.get("artifacts") if isinstance(bundle.get("artifacts"), dict) else {}
                    self.database.upsert_oos_scenario(
                        {
                            "oos_run_id": oos_run_id,
                            "scenario_index": index,
                            "scenario_key": spec.key,
                            "scenario_label": spec.label,
                            "source": spec.source,
                            "status": JobState.COMPLETED.value,
                            "started_at": scenario_row.get("started_at") if scenario_row else utc_now().isoformat(),
                            "finished_at": utc_now().isoformat(),
                            "run_id": run_payload.get("id"),
                            "export_path": artifacts.get("run_dir"),
                            "bundle_path": artifacts.get("bundle_json") or row.get("baseline_bundle_path"),
                            "report_path": artifacts.get("summary"),
                            "warning_json": list(result.warnings),
                            "error": None,
                            "metadata_json": deepcopy(result.metadata),
                        }
                    )
                    completed_results.append(result)
                    self.database.update_oos_run(
                        oos_run_id,
                        {
                            "progress_current": len(completed_results),
                            "last_heartbeat_at": utc_now().isoformat(),
                        },
                    )
                    if callable(progress_callback):
                        progress_callback(oos_run_id, len(completed_results), len(specs), spec.key)
                    self._log(
                        "oos_scenario_completed",
                        {
                            "oos_run_id": oos_run_id,
                            "scenario_key": spec.key,
                            "run_id": run_payload.get("id"),
                            "export_path": artifacts.get("run_dir"),
                        },
                    )
                except Exception as exc:
                    failed = True
                    self.database.upsert_oos_scenario(
                        {
                            "oos_run_id": oos_run_id,
                            "scenario_index": index,
                            "scenario_key": spec.key,
                            "scenario_label": spec.label,
                            "source": spec.source,
                            "status": JobState.FAILED.value,
                            "finished_at": utc_now().isoformat(),
                            "warning_json": [],
                            "error": str(exc),
                            "metadata_json": {"description": spec.description},
                        }
                    )
                    self._log(
                        "oos_scenario_failed",
                        {"oos_run_id": oos_run_id, "scenario_key": spec.key, "error": str(exc)},
                    )
            final_state = JobState.COMPLETED.value
            if failed and completed_results:
                final_state = JobState.PARTIAL.value
            elif failed and not completed_results:
                final_state = JobState.FAILED.value
            report_error = None
            report_path = None
            if completed_results:
                try:
                    report = framework.build_report(request, completed_results)
                    framework.write_report(output_dir, report)
                    report_path = str(Path(output_dir) / "report.json")
                    self._log("oos_report_completed", {"oos_run_id": oos_run_id, "report_path": report_path})
                except Exception as exc:
                    report_error = str(exc)
                    final_state = JobState.PARTIAL.value if completed_results else JobState.FAILED.value
                    self._log("oos_report_failed", {"oos_run_id": oos_run_id, "error": report_error})
            else:
                report_error = "no_completed_scenarios"
            self.database.update_oos_run(
                oos_run_id,
                {
                    "state": final_state,
                    "finished_at": utc_now().isoformat(),
                    "progress_current": len(completed_results),
                    "progress_total": len(specs),
                    "last_heartbeat_at": utc_now().isoformat(),
                    "report_status": "completed" if report_path and report_error is None else "failed",
                    "report_path": report_path,
                    "report_error": report_error,
                    "failure_reason": report_error if final_state == JobState.FAILED.value else None,
                },
            )
        except InterruptedError as exc:
            self.database.update_oos_run(
                oos_run_id,
                {
                    "state": JobState.INTERRUPTED.value,
                    "finished_at": utc_now().isoformat(),
                    "interrupted_reason": str(exc) or "keyboard_interrupt",
                },
            )
            self._log("oos_run_interrupted", {"oos_run_id": oos_run_id, "error": str(exc)})
            raise
        except KeyboardInterrupt as exc:
            self.database.update_oos_run(
                oos_run_id,
                {
                    "state": JobState.INTERRUPTED.value,
                    "finished_at": utc_now().isoformat(),
                    "interrupted_reason": str(exc) or "keyboard_interrupt",
                },
            )
            self._log("oos_run_interrupted", {"oos_run_id": oos_run_id, "error": str(exc)})
            raise
        except Exception as exc:
            scenarios = self.database.get_oos_scenarios(oos_run_id)
            completed = sum(1 for item in scenarios if normalize_job_state(item.get("status")) == JobState.COMPLETED.value)
            state = JobState.PARTIAL.value if completed else JobState.FAILED.value
            self.database.update_oos_run(
                oos_run_id,
                {
                    "state": state,
                    "finished_at": utc_now().isoformat(),
                    "failure_reason": str(exc),
                    "progress_current": completed,
                    "progress_total": len(scenarios),
                },
            )
            self._log("oos_run_failed", {"oos_run_id": oos_run_id, "error": str(exc)})
            raise
        return self.get_status(oos_run_id)

    def get_status(self, oos_run_id: int | None = None) -> dict[str, Any]:
        """Return the current or most recent OOS run payload."""
        self._reconcile_stale_runs()
        row = self.database.get_oos_run(oos_run_id) if oos_run_id is not None else None
        if row is None:
            rows = self.database.get_oos_runs(limit=1)
            row = rows[0] if rows else None
        if row is None:
            return {
                "ok": True,
                "run": None,
                "history": [],
                "active_run_id": None,
                "state": JobState.IDLE.value,
            }
        payload = self._run_payload(row)
        return {
            "ok": True,
            "run": payload,
            "history": self.history(limit=10)["runs"],
            "active_run_id": payload["id"] if payload.get("active") else None,
            "state": payload["state"],
        }

    def history(self, limit: int = 20) -> dict[str, Any]:
        """Return recent OOS runs with nested scenario summaries."""
        self._reconcile_stale_runs()
        rows = self.database.get_oos_runs(limit=limit)
        return {"ok": True, "runs": [self._run_payload(row) for row in rows]}
