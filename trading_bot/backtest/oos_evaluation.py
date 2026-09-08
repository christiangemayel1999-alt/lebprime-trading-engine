from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from services.backtest_runner import BacktestRunner
from services.backtest_storage import BacktestStorage
from services.database import DatabaseService
from trading_bot.config.schema import ConfigSchema
from trading_bot.core.execution_mode import (
    V1_BASELINE,
    V2_ADAPTIVE_ONLY,
    V2_FAMILY_MANAGEMENT_ONLY,
    V2_FULL,
    V2_NO_LEBPRIM,
    apply_execution_mode_to_config,
    mode_label,
)
from trading_bot.core.families import strategy_family_for_setup
from utils import ensure_directory, load_json, save_json_atomic, to_utc, utc_now


DEFAULT_ENABLED_STRATEGIES = [
    "XAU_BOT_BREAKOUT",
    "XAU_BOT_COMPRESS",
    "XAU_LEBPRIM",
]
DEFERRED_ACTIONS = {"WAIT_RETEST", "PLACE_LIMIT"}
LIMIT_ENTRY_MODES = {"lebprim_limit", "limit_value", "wait_retest"}


@dataclass(slots=True)
class ScenarioSpec:
    key: str
    label: str
    description: str
    execution_mode: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    enabled_strategies: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_STRATEGIES))
    source: str = "run"
    baseline_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScenarioResult:
    key: str
    label: str
    source: str
    bundle: dict[str, Any]
    description: str
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
    return target


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "None"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _nested(mapping: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    current: Any = mapping or {}
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _price_tolerance(entry_price: float, stop_loss: float, target_price: float) -> float:
    risk_price = abs(float(entry_price) - float(stop_loss))
    target_distance = abs(float(target_price) - float(entry_price))
    return max(0.05, risk_price * 0.05, target_distance * 0.02)


def _to_iso_date(value: Any) -> str:
    dt = to_utc(value)
    return dt.date().isoformat() if dt is not None else ""


def _to_iso_ts(value: Any) -> str:
    dt = to_utc(value)
    return dt.isoformat() if dt is not None else ""


def _profit_factor(profits: float, losses: float) -> float:
    if losses > 0:
        return profits / losses
    return 999.0 if profits > 0 else 0.0


def _win_rate(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float((series > 0).mean() * 100.0)


def _scenario_markdown_table(frame: pd.DataFrame, decimals: int = 2) -> str:
    if frame.empty:
        return "_No data_"
    formatted = frame.copy()
    for column in formatted.columns:
        if pd.api.types.is_float_dtype(formatted[column]):
            formatted[column] = formatted[column].map(lambda item: f"{item:.{decimals}f}")
        else:
            formatted[column] = formatted[column].astype(str)
    header = "| " + " | ".join(str(col) for col in formatted.columns) + " |"
    separator = "| " + " | ".join("---" for _ in formatted.columns) + " |"
    rows = ["| " + " | ".join(str(value) for value in row) + " |" for row in formatted.to_numpy().tolist()]
    return "\n".join([header, separator, *rows])


class OOSEvaluationFramework:
    """Run and compare structured out-of-sample scenario matrices for Bot V2."""

    def __init__(self, project_root: str | Path, config: dict[str, Any], database_path: str | Path | None = None) -> None:
        self.project_root = Path(project_root)
        self.base_config = ConfigSchema.normalize(deepcopy(config))
        default_db_path = self.project_root / self.base_config.get("storage", {}).get("database_path", "storage/trading_bot.db")
        self.database_path = Path(database_path) if database_path else Path(default_db_path)

    @staticmethod
    def default_scenarios(
        *,
        baseline_run_id: int | None = None,
        baseline_bundle_path: str | Path | None = None,
    ) -> list[ScenarioSpec]:
        scenarios: list[ScenarioSpec] = []
        if baseline_run_id is not None:
            scenarios.append(
                ScenarioSpec(
                    key="bot_v1_baseline",
                    label="Bot V1 Baseline",
                    description="External baseline loaded from an existing backtest run id.",
                    execution_mode=V1_BASELINE,
                    source="external_run_id",
                    baseline_ref=str(int(baseline_run_id)),
                    metadata={"baseline_source": "run_id", "requested_execution_mode": V1_BASELINE},
                )
            )
        elif baseline_bundle_path is not None:
            scenarios.append(
                ScenarioSpec(
                    key="bot_v1_baseline",
                    label="Bot V1 Baseline",
                    description="External baseline loaded from an exported bundle.json path.",
                    execution_mode=V1_BASELINE,
                    source="external_bundle",
                    baseline_ref=str(baseline_bundle_path),
                    metadata={"baseline_source": "bundle_path", "requested_execution_mode": V1_BASELINE},
                )
            )
        else:
            scenarios.append(
                ScenarioSpec(
                    key="bot_v1_baseline",
                    label="Bot V1 Baseline",
                    description="Emulated baseline using the strict V1 baseline execution mode.",
                    execution_mode=V1_BASELINE,
                    metadata={"baseline_source": "emulated_from_mode_contract", "requested_execution_mode": V1_BASELINE},
                )
            )
        scenarios.extend(
            [
                ScenarioSpec(
                    key="bot_v2_full",
                    label="Bot V2 Full",
                    description="All Bot V2 controls enabled, including adaptive execution, family-specific management, restricted LEBPRIM, and controlled dual exposure in backtest.",
                    execution_mode=V2_FULL,
                    overrides={
                        "execution": {"allow_multi_position": True, "allow_same_direction_multi_strategy": True},
                        "exit": {"enable_family_specific_management": True},
                    },
                    metadata={"requested_execution_mode": V2_FULL},
                ),
                ScenarioSpec(
                    key="bot_v2_lebprim_disabled",
                    label="Bot V2 With LEBPRIM Disabled",
                    description="Bot V2 with LEBPRIM fully removed from participation.",
                    execution_mode=V2_NO_LEBPRIM,
                    enabled_strategies=["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS"],
                    metadata={"requested_execution_mode": V2_NO_LEBPRIM},
                ),
                ScenarioSpec(
                    key="bot_v2_multi_position_disabled",
                    label="Bot V2 With Multi-Position Disabled",
                    description="Bot V2 with adaptive execution and family-specific management preserved, but dual exposure disabled.",
                    execution_mode=V2_FULL,
                    overrides={"execution": {"allow_multi_position": False, "allow_same_direction_multi_strategy": False}},
                    metadata={"requested_execution_mode": V2_FULL},
                ),
                ScenarioSpec(
                    key="bot_v2_adaptive_only",
                    label="Bot V2 Adaptive Execution Only",
                    description="Adaptive execution stays enabled while family-specific management falls back to the generic profile.",
                    execution_mode=V2_ADAPTIVE_ONLY,
                    metadata={"requested_execution_mode": V2_ADAPTIVE_ONLY},
                ),
                ScenarioSpec(
                    key="bot_v2_family_management_only",
                    label="Bot V2 Family Management Only",
                    description="Family-specific management stays enabled while adaptive execution falls back to the legacy entry passthrough.",
                    execution_mode=V2_FAMILY_MANAGEMENT_ONLY,
                    metadata={"requested_execution_mode": V2_FAMILY_MANAGEMENT_ONLY},
                ),
            ]
        )
        return scenarios

    def run_matrix(
        self,
        *,
        request: dict[str, Any],
        output_dir: str | Path | None = None,
        baseline_run_id: int | None = None,
        baseline_bundle_path: str | Path | None = None,
        baseline_database_path: str | Path | None = None,
    ) -> dict[str, Any]:
        out_dir = ensure_directory(
            output_dir
            or self.project_root / "reports" / f"oos_eval_{utc_now().strftime('%Y%m%d_%H%M%S')}"
        )
        scenarios = self.default_scenarios(
            baseline_run_id=baseline_run_id,
            baseline_bundle_path=baseline_bundle_path,
        )
        scenario_results: list[ScenarioResult] = []
        for spec in scenarios:
            scenario_results.append(
                self._materialize_scenario(
                    spec=spec,
                    request=request,
                    baseline_database_path=baseline_database_path,
                )
            )
        report = self._build_report(request, scenario_results)
        self.write_report(out_dir, report)
        report["output_dir"] = str(out_dir)
        return report

    def materialize_scenario(
        self,
        *,
        spec: ScenarioSpec,
        request: dict[str, Any],
        baseline_database_path: str | Path | None = None,
    ) -> ScenarioResult:
        """Public wrapper for materializing one scenario result."""
        return self._materialize_scenario(
            spec=spec,
            request=request,
            baseline_database_path=baseline_database_path,
        )

    def build_report(self, request: dict[str, Any], scenario_results: list[ScenarioResult]) -> dict[str, Any]:
        """Public wrapper for assembling a report from scenario results."""
        return self._build_report(request, scenario_results)

    def write_report(self, output_dir: str | Path, report: dict[str, Any]) -> None:
        """Public wrapper for exporting report artifacts."""
        self._write_report(Path(output_dir), report)

    def _materialize_scenario(
        self,
        *,
        spec: ScenarioSpec,
        request: dict[str, Any],
        baseline_database_path: str | Path | None,
    ) -> ScenarioResult:
        if spec.source == "external_bundle":
            bundle = self._load_bundle_from_path(Path(str(spec.baseline_ref or "")))
            warnings = self._run_window_warnings(request, bundle)
            metadata = deepcopy(spec.metadata)
            metadata.setdefault(
                "resolved_execution_mode",
                {"selected_mode": spec.execution_mode or "", "label": mode_label(str(spec.execution_mode or ""))},
            )
            return ScenarioResult(
                key=spec.key,
                label=spec.label,
                source=spec.source,
                bundle=bundle,
                description=spec.description,
                warnings=warnings,
                metadata=metadata,
            )
        if spec.source == "external_run_id":
            db_path = Path(baseline_database_path) if baseline_database_path else self.database_path
            storage = BacktestStorage(DatabaseService(db_path))
            bundle = storage.get_run_bundle(int(str(spec.baseline_ref or "0")))
            warnings = self._run_window_warnings(request, bundle)
            metadata = deepcopy(spec.metadata)
            metadata["baseline_database_path"] = str(db_path)
            metadata.setdefault(
                "resolved_execution_mode",
                {"selected_mode": spec.execution_mode or "", "label": mode_label(str(spec.execution_mode or ""))},
            )
            return ScenarioResult(
                key=spec.key,
                label=spec.label,
                source=spec.source,
                bundle=bundle,
                description=spec.description,
                warnings=warnings,
                metadata=metadata,
            )

        scenario_config, resolved_mode = self._scenario_config(spec.execution_mode, spec.overrides)
        scenario_database = DatabaseService(self.database_path)
        runner = BacktestRunner(self.project_root, scenario_config, scenario_database)
        run_request = deepcopy(request)
        run_request["enabled_strategies"] = list(spec.enabled_strategies)
        run_request["engine_mode"] = resolved_mode.selected_mode
        run_request["notes"] = f"{spec.key}|{spec.label}"
        bundle = runner.run(run_request)
        metadata = deepcopy(spec.metadata)
        metadata["enabled_strategies"] = list(spec.enabled_strategies)
        metadata["scenario_overrides"] = deepcopy(spec.overrides)
        metadata["resolved_execution_mode"] = resolved_mode.to_dict()
        return ScenarioResult(
            key=spec.key,
            label=spec.label,
            source=spec.source,
            bundle=bundle,
            description=spec.description,
            warnings=self._run_window_warnings(request, bundle),
            metadata=metadata,
        )

    def _scenario_config(self, execution_mode: str | None, overrides: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        cfg = ConfigSchema.normalize(deepcopy(self.base_config))
        cfg, resolved_mode = apply_execution_mode_to_config(cfg, execution_mode or cfg.get("bot", {}).get("execution_mode"))
        _deep_merge(cfg, overrides)
        cfg, resolved_mode = apply_execution_mode_to_config(cfg, execution_mode or resolved_mode.selected_mode)
        return ConfigSchema.normalize(cfg), resolved_mode

    @staticmethod
    def _load_bundle_from_path(path: Path) -> dict[str, Any]:
        bundle_path = path / "bundle.json" if path.is_dir() else path
        bundle = load_json(bundle_path, {})
        if not isinstance(bundle, dict) or not bundle:
            raise ValueError(f"Bundle not found or invalid: {bundle_path}")
        return bundle

    @staticmethod
    def _run_window_warnings(request: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        run_payload = bundle.get("run", {}) if isinstance(bundle.get("run"), dict) else {}
        expected_start = str(request.get("start_date") or "")
        expected_end = str(request.get("end_date") or request.get("start_date") or "")
        actual_start = str(run_payload.get("start_date") or "")
        actual_end = str(run_payload.get("end_date") or "")
        if expected_start and expected_start not in actual_start:
            warnings.append(f"requested_start={expected_start} differs from run_start={actual_start}")
        if expected_end and expected_end not in actual_end:
            warnings.append(f"requested_end={expected_end} differs from run_end={actual_end}")
        return warnings

    def _build_report(self, request: dict[str, Any], scenario_results: list[ScenarioResult]) -> dict[str, Any]:
        scenario_frames = {result.key: self._scenario_frames(result) for result in scenario_results}
        portfolio = self._portfolio_summary(scenario_results, scenario_frames)
        signal_funnel = self._signal_funnel_summary(scenario_results, scenario_frames)
        execution_quality = self._execution_quality_summary(scenario_results, scenario_frames)
        management_quality = self._management_quality_summary(scenario_results, scenario_frames)
        blocked_reasons = self._blocked_reason_breakdown(scenario_results, scenario_frames)
        executed_rate_by_strategy = self._executed_rate_by_strategy(scenario_results, scenario_frames)
        score_distribution = self._score_distribution(scenario_results, scenario_frames)
        family_contribution = self._family_contribution(scenario_results, scenario_frames)
        family_session = self._family_breakdown(scenario_results, scenario_frames, "session_name")
        family_regime = self._family_breakdown(scenario_results, scenario_frames, "regime_name")
        family_volatility = self._family_breakdown(scenario_results, scenario_frames, "volatility_bucket")
        daily_consistency = self._daily_consistency(scenario_results, scenario_frames)
        session_consistency = self._session_consistency(scenario_results, scenario_frames)
        exit_reason_distribution = self._exit_reason_distribution(scenario_results, scenario_frames)
        deferred_conversion = self._deferred_conversion(scenario_results, scenario_frames)
        scenario_comparison = self._scenario_comparison(portfolio, signal_funnel, execution_quality, management_quality)
        quality_verdicts = self._quality_vs_participation(portfolio, signal_funnel, execution_quality, management_quality)
        manifest = [
            {
                "scenario_key": result.key,
                "scenario_label": result.label,
                "source": result.source,
                "description": result.description,
                "run_id": _nested(result.bundle, "run", "id"),
                "run_dir": _nested(result.bundle, "artifacts", "run_dir"),
                "warnings": list(result.warnings),
                "metadata": deepcopy(result.metadata),
                "requested_execution_mode": str((result.metadata or {}).get("requested_execution_mode") or ""),
                "resolved_execution_mode_label": mode_label(
                    str((result.metadata or {}).get("resolved_execution_mode", {}).get("selected_mode") or (result.metadata or {}).get("requested_execution_mode") or "")
                ),
            }
            for result in scenario_results
        ]
        return {
            "generated_at": utc_now().isoformat(),
            "request": deepcopy(request),
            "scenario_manifest": manifest,
            "tables": {
                "portfolio_summary": portfolio,
                "signal_funnel": signal_funnel,
                "execution_quality": execution_quality,
                "management_quality": management_quality,
                "scenario_comparison": scenario_comparison,
                "blocked_reason_breakdown": blocked_reasons,
                "executed_rate_by_strategy": executed_rate_by_strategy,
                "score_distribution": score_distribution,
                "family_contribution": family_contribution,
                "family_session_breakdown": family_session,
                "family_regime_breakdown": family_regime,
                "family_volatility_breakdown": family_volatility,
                "daily_consistency": daily_consistency,
                "session_consistency": session_consistency,
                "exit_reason_distribution": exit_reason_distribution,
                "deferred_conversion": deferred_conversion,
            },
            "quality_verdicts": quality_verdicts,
        }

    def _scenario_frames(self, scenario: ScenarioResult) -> dict[str, pd.DataFrame]:
        return {
            "signals": self._signals_frame(scenario),
            "trades": self._trades_frame(scenario),
            "equity": self._equity_frame(scenario),
        }

    def _signals_frame(self, scenario: ScenarioResult) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for item in scenario.bundle.get("signals", []):
            raw = item.get("raw_signal_json") if isinstance(item.get("raw_signal_json"), dict) else {}
            candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
            entry = raw.get("entry") if isinstance(raw.get("entry"), dict) else {}
            market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
            decision = raw.get("execution_decision") if isinstance(raw.get("execution_decision"), dict) else {}
            diagnostics = raw.get("diagnostics") if isinstance(raw.get("diagnostics"), dict) else {}
            regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
            session = market.get("session") if isinstance(market.get("session"), dict) else {}
            decision_meta = decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {}
            execution_action = str(decision.get("action") or "")
            signal_status = str(diagnostics.get("signal_status") or "")
            entry_mode = str(_coalesce(entry.get("entry_mode"), diagnostics.get("entry_mode"), "") or "")
            if not execution_action:
                if _safe_bool(item.get("executed")):
                    execution_action = "EXECUTE_NOW"
                elif entry_mode in LIMIT_ENTRY_MODES:
                    execution_action = "PLACE_LIMIT"
                else:
                    execution_action = "BLOCK"
            blocked_reason = str(
                _coalesce(
                    diagnostics.get("blocked_reason"),
                    item.get("blocked_reason"),
                    decision.get("reason_code") if execution_action == "BLOCK" else None,
                )
                or ""
            )
            if blocked_reason in {"", "entry_valid"} and signal_status in {"rejected", "cancelled"}:
                blocked_reason = str(_coalesce(item.get("blocked_reason"), entry.get("reason_code"), decision.get("reason_code")) or "")
            rows.append(
                {
                    "scenario_key": scenario.key,
                    "scenario_label": scenario.label,
                    "signal_time": _to_iso_ts(item.get("signal_time")),
                    "signal_date": _to_iso_date(item.get("signal_time")),
                    "strategy_name": str(item.get("strategy_name") or ""),
                    "setup_family": str(item.get("setup") or candidate.get("setup_family") or ""),
                    "strategy_family": str(
                        _coalesce(
                            decision_meta.get("strategy_family"),
                            strategy_family_for_setup(str(item.get("setup") or candidate.get("setup_family") or "")),
                        )
                        or ""
                    ),
                    "side": str(item.get("side") or candidate.get("side") or ""),
                    "qualified": _safe_bool(_coalesce(candidate.get("setup_valid"), diagnostics.get("setup_valid"), entry.get("valid"), False)),
                    "executed": _safe_bool(item.get("executed")),
                    "execution_reason": str(item.get("execution_reason") or ""),
                    "blocked_reason": blocked_reason,
                    "blocker_source": str(diagnostics.get("blocker_source") or ""),
                    "execution_action": execution_action,
                    "signal_status": signal_status,
                    "entry_mode": entry_mode,
                    "entry_score": _safe_float(_coalesce(entry.get("entry_score"), diagnostics.get("entry_score"), item.get("score"))),
                    "trigger_score": _safe_float(_coalesce(entry.get("trigger_score"), diagnostics.get("trigger_score"))),
                    "setup_score": _safe_float(_coalesce(entry.get("setup_score"), candidate.get("setup_score"))),
                    "trend_score": _safe_float(_coalesce(entry.get("trend_score"), candidate.get("trend_score"))),
                    "value_distance_atr": _safe_float(entry.get("value_distance_atr")),
                    "trigger_candle_atr": _safe_float(entry.get("trigger_candle_atr")),
                    "quality_tier": str(_coalesce(diagnostics.get("quality_tier"), decision_meta.get("quality_tier"), "")),
                    "management_profile": str(_coalesce(diagnostics.get("management_profile"), decision_meta.get("management_profile"), "")),
                    "planned_entry_price": _safe_float(
                        _coalesce(
                            decision.get("entry_price"),
                            diagnostics.get("pending_entry_price"),
                            entry.get("pending_entry_price"),
                            entry.get("entry_price"),
                            item.get("entry"),
                        )
                    ),
                    "reference_price": _safe_float(_coalesce(decision.get("reference_price"), candidate.get("value_price"))),
                    "setup_fingerprint": str(candidate.get("setup_fingerprint") or ""),
                    "regime_name": str(_coalesce(candidate.get("regime_name"), regime.get("regime_name"), "") or ""),
                    "regime_score": _safe_float(_coalesce(candidate.get("regime_confidence"), regime.get("regime_confidence"))),
                    "session_name": str(_coalesce(candidate.get("session_name"), session.get("session_name"), "") or ""),
                    "session_quality_score": _safe_float(_coalesce(candidate.get("session_quality_score"), session.get("session_quality_score"))),
                    "volatility_bucket": str(_coalesce(decision_meta.get("volatility_state"), "unknown")),
                    "hard_blocked": execution_action == "BLOCK",
                    "deferred": execution_action in DEFERRED_ACTIONS,
                    "pending_created": str(item.get("execution_reason") or "") == "pending_order_created" or signal_status == "pending",
                    "final_blocked": bool(blocked_reason),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(
                columns=[
                    "scenario_key",
                    "scenario_label",
                    "signal_time",
                    "strategy_name",
                    "setup_family",
                    "strategy_family",
                    "side",
                    "qualified",
                    "executed",
                    "execution_action",
                    "blocked_reason",
                ]
            )
        return frame

    def _trades_frame(self, scenario: ScenarioResult) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for item in scenario.bundle.get("trades", []):
            meta = item.get("trade_metadata_json") if isinstance(item.get("trade_metadata_json"), dict) else {}
            candidate = meta.get("candidate") if isinstance(meta.get("candidate"), dict) else {}
            entry = meta.get("entry") if isinstance(meta.get("entry"), dict) else {}
            market = meta.get("market") if isinstance(meta.get("market"), dict) else {}
            regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
            session = market.get("session") if isinstance(market.get("session"), dict) else {}
            management_events = meta.get("management_events") if isinstance(meta.get("management_events"), list) else []
            entry_price = _safe_float(item.get("entry"))
            stop_loss = _safe_float(item.get("sl"))
            take_profit = _safe_float(item.get("tp"))
            tp1 = _safe_float(_coalesce(meta.get("tp1"), item.get("tp1"), 0.0))
            exit_price = _safe_float(item.get("exit_price"))
            risk_price = max(abs(entry_price - stop_loss), 1e-9)
            tp1_hit = any(str(event.get("reason_code") or "") == "partial_tp1" for event in management_events)
            if not tp1_hit and tp1 > 0:
                tp1_hit = _safe_float(item.get("mfe")) >= max(abs(tp1 - entry_price) - _price_tolerance(entry_price, stop_loss, tp1), 0.0)
            final_tp_hit = str(item.get("exit_reason_code") or "").lower() in {"take_profit_hit", "final_tp_hit", "tp_hit", "take_profit"}
            if not final_tp_hit and take_profit > 0:
                final_tp_hit = abs(exit_price - take_profit) <= _price_tolerance(entry_price, stop_loss, take_profit)
            breakeven_moved = any(str(event.get("reason_code") or "") == "breakeven_after_tp1" for event in management_events)
            execution_action = str(meta.get("execution_decision") or ("PLACE_LIMIT" if str(meta.get("entry_mode") or "") in LIMIT_ENTRY_MODES else "EXECUTE_NOW"))
            planned_entry = _safe_float(_coalesce(meta.get("pending_entry_price"), entry.get("entry_price"), candidate.get("entry_price"), entry_price))
            fill_delta = abs(entry_price - planned_entry) if planned_entry > 0 else 0.0
            rows.append(
                {
                    "scenario_key": scenario.key,
                    "scenario_label": scenario.label,
                    "open_time": _to_iso_ts(item.get("open_time")),
                    "close_time": _to_iso_ts(item.get("close_time")),
                    "close_date": _to_iso_date(item.get("close_time")),
                    "strategy_name": str(item.get("strategy_name") or ""),
                    "setup_family": str(_coalesce(meta.get("setup_family"), item.get("strategy_name"), "")),
                    "strategy_family": str(
                        _coalesce(
                            meta.get("strategy_family"),
                            strategy_family_for_setup(str(_coalesce(meta.get("setup_family"), candidate.get("setup_family"), ""))),
                        )
                        or ""
                    ),
                    "side": str(item.get("side") or candidate.get("side") or ""),
                    "entry_mode": str(meta.get("entry_mode") or ""),
                    "execution_action": execution_action,
                    "quality_tier": str(meta.get("quality_tier") or ""),
                    "management_profile": str(meta.get("management_profile") or ""),
                    "volatility_bucket": str(_coalesce(meta.get("volatility_state"), "unknown")),
                    "entry": entry_price,
                    "planned_entry_price": planned_entry,
                    "fill_delta_price": fill_delta,
                    "fill_delta_r": fill_delta / risk_price,
                    "sl": stop_loss,
                    "tp": take_profit,
                    "tp1": tp1,
                    "exit_price": exit_price,
                    "pnl": _safe_float(item.get("pnl")),
                    "pnl_r": _safe_float(item.get("pnl_r")),
                    "mfe": _safe_float(item.get("mfe")),
                    "mae": _safe_float(item.get("mae")),
                    "mfe_r": _safe_float(item.get("mfe")) / risk_price,
                    "mae_r": _safe_float(item.get("mae")) / risk_price,
                    "duration_seconds": _safe_float(item.get("duration_seconds")),
                    "exit_reason_code": str(item.get("exit_reason_code") or ""),
                    "exit_reason": str(item.get("exit_reason") or ""),
                    "tp1_hit": tp1_hit,
                    "final_tp_hit": final_tp_hit,
                    "breakeven_moved": breakeven_moved,
                    "momentum_failure_exit": str(item.get("exit_reason_code") or "") == "momentum_failure_exit",
                    "pending_fill": bool(meta.get("pending_order_id") or meta.get("filled_from_pending_order")),
                    "wait_retest_fill": execution_action == "WAIT_RETEST" or str(meta.get("entry_mode") or "") == "wait_retest",
                    "limit_fill": str(meta.get("entry_mode") or "") in LIMIT_ENTRY_MODES,
                    "entry_score": _safe_float(_coalesce(entry.get("entry_score"), candidate.get("setup_score"))),
                    "value_distance_atr": _safe_float(entry.get("value_distance_atr")),
                    "setup_fingerprint": str(_coalesce(meta.get("setup_fingerprint"), candidate.get("setup_fingerprint"), "")),
                    "regime_name": str(_coalesce(candidate.get("regime_name"), regime.get("regime_name"), "")),
                    "session_name": str(_coalesce(candidate.get("session_name"), session.get("session_name"), "")),
                }
            )
        return pd.DataFrame(rows)

    def _equity_frame(self, scenario: ScenarioResult) -> pd.DataFrame:
        frame = pd.DataFrame(
            [
                {
                    "scenario_key": scenario.key,
                    "scenario_label": scenario.label,
                    "ts": _to_iso_ts(item.get("ts")),
                    "equity": _safe_float(item.get("equity")),
                    "balance": _safe_float(item.get("balance")),
                    "floating_pnl": _safe_float(item.get("floating_pnl")),
                }
                for item in scenario.bundle.get("equity_curve", [])
            ]
        )
        if frame.empty:
            return pd.DataFrame(columns=["scenario_key", "scenario_label", "ts", "equity", "balance", "floating_pnl"])
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        return frame

    def _portfolio_summary(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            equity = scenario_frames[result.key]["equity"]
            run_payload = result.bundle.get("run", {}) if isinstance(result.bundle.get("run"), dict) else {}
            start_dt = to_utc(run_payload.get("start_date"))
            end_dt = to_utc(run_payload.get("end_date"))
            period_days = max(((end_dt.date() - start_dt.date()).days + 1), 1) if start_dt and end_dt else max(int(trades["close_date"].nunique()), 1) if not trades.empty else 1
            gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum()) if not trades.empty else 0.0
            gross_loss = abs(float(trades.loc[trades["pnl"] < 0, "pnl"].sum())) if not trades.empty else 0.0
            net_profit = gross_profit - gross_loss
            max_drawdown = 0.0
            if not equity.empty:
                running_peak = equity["equity"].cummax()
                drawdown = ((running_peak - equity["equity"]) / running_peak.replace(0.0, pd.NA)).fillna(0.0) * 100.0
                max_drawdown = float(drawdown.max())
            daily_pnl = (
                trades.groupby("close_date", dropna=False)["pnl"].sum().rename("daily_pnl").reset_index()
                if not trades.empty
                else pd.DataFrame(columns=["close_date", "daily_pnl"])
            )
            positive_days = int((daily_pnl["daily_pnl"] > 0).sum()) if not daily_pnl.empty else 0
            rows.append(
                {
                    "scenario_key": result.key,
                    "scenario_label": result.label,
                    "source": result.source,
                    "net_profit": net_profit,
                    "gross_profit": gross_profit,
                    "gross_loss": gross_loss,
                    "profit_factor": _profit_factor(gross_profit, gross_loss),
                    "total_trades": int(len(trades)),
                    "win_rate": _win_rate(trades["pnl"]) if not trades.empty else 0.0,
                    "average_trade": float(trades["pnl"].mean()) if not trades.empty else 0.0,
                    "average_r": float(trades["pnl_r"].mean()) if not trades.empty else 0.0,
                    "max_drawdown": max_drawdown,
                    "average_daily_pnl": net_profit / max(period_days, 1),
                    "positive_day_rate": (positive_days / max(period_days, 1)) * 100.0,
                    "daily_pnl_std": float(daily_pnl["daily_pnl"].std(ddof=0)) if not daily_pnl.empty else 0.0,
                    "best_day_pnl": float(daily_pnl["daily_pnl"].max()) if not daily_pnl.empty else 0.0,
                    "worst_day_pnl": float(daily_pnl["daily_pnl"].min()) if not daily_pnl.empty else 0.0,
                    "period_days": int(period_days),
                    "run_id": _nested(result.bundle, "run", "id"),
                }
            )
        return pd.DataFrame(rows).sort_values("scenario_label").reset_index(drop=True)

    def _signal_funnel_summary(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            total_signals = int(len(signals))
            qualified = int(signals["qualified"].sum()) if not signals.empty else 0
            executed = int(signals["executed"].sum()) if not signals.empty else 0
            deferred = int(signals["deferred"].sum()) if not signals.empty else 0
            hard_blocked = int(signals["hard_blocked"].sum()) if not signals.empty else 0
            final_blocked = int(signals["final_blocked"].sum()) if not signals.empty else 0
            rows.append(
                {
                    "scenario_key": result.key,
                    "scenario_label": result.label,
                    "total_signals": total_signals,
                    "qualified_signals": qualified,
                    "executed_signals": executed,
                    "deferred_signals": deferred,
                    "blocked_signals": final_blocked,
                    "hard_blocked_signals": hard_blocked,
                    "qualified_rate_pct": (qualified / max(total_signals, 1)) * 100.0,
                    "executed_rate_pct": (executed / max(total_signals, 1)) * 100.0,
                    "deferred_rate_pct": (deferred / max(total_signals, 1)) * 100.0,
                    "block_rate_pct": (final_blocked / max(total_signals, 1)) * 100.0,
                }
            )
        return pd.DataFrame(rows).sort_values("scenario_label").reset_index(drop=True)

    def _execution_quality_summary(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            trades = scenario_frames[result.key]["trades"]
            wait_signals = signals.loc[signals["execution_action"] == "WAIT_RETEST"] if not signals.empty else pd.DataFrame()
            limit_signals = signals.loc[signals["execution_action"] == "PLACE_LIMIT"] if not signals.empty else pd.DataFrame()
            wait_trades = trades.loc[trades["wait_retest_fill"]] if not trades.empty else pd.DataFrame()
            limit_trades = trades.loc[trades["limit_fill"]] if not trades.empty else pd.DataFrame()
            rows.append(
                {
                    "scenario_key": result.key,
                    "scenario_label": result.label,
                    "avg_entry_distance_from_value_atr": float(trades["value_distance_atr"].mean()) if not trades.empty else 0.0,
                    "median_entry_distance_from_value_atr": float(trades["value_distance_atr"].median()) if not trades.empty else 0.0,
                    "avg_fill_delta_price": float(trades["fill_delta_price"].mean()) if not trades.empty else 0.0,
                    "avg_fill_delta_r": float(trades["fill_delta_r"].mean()) if not trades.empty else 0.0,
                    "retest_conversion_rate": (len(wait_trades) / max(len(wait_signals), 1)) * 100.0,
                    "limit_conversion_rate": (len(limit_trades) / max(len(limit_signals), 1)) * 100.0,
                    "wait_retest_win_rate": _win_rate(wait_trades["pnl"]) if not wait_trades.empty else 0.0,
                    "wait_retest_profit_factor": _profit_factor(
                        float(wait_trades.loc[wait_trades["pnl"] > 0, "pnl"].sum()) if not wait_trades.empty else 0.0,
                        abs(float(wait_trades.loc[wait_trades["pnl"] < 0, "pnl"].sum())) if not wait_trades.empty else 0.0,
                    ),
                    "wait_retest_avg_trade": float(wait_trades["pnl"].mean()) if not wait_trades.empty else 0.0,
                    "limit_win_rate": _win_rate(limit_trades["pnl"]) if not limit_trades.empty else 0.0,
                    "limit_profit_factor": _profit_factor(
                        float(limit_trades.loc[limit_trades["pnl"] > 0, "pnl"].sum()) if not limit_trades.empty else 0.0,
                        abs(float(limit_trades.loc[limit_trades["pnl"] < 0, "pnl"].sum())) if not limit_trades.empty else 0.0,
                    ),
                    "limit_avg_trade": float(limit_trades["pnl"].mean()) if not limit_trades.empty else 0.0,
                }
            )
        return pd.DataFrame(rows).sort_values("scenario_label").reset_index(drop=True)

    def _management_quality_summary(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            tp1_hits = int(trades["tp1_hit"].sum()) if not trades.empty else 0
            breakeven_moves = int(trades["breakeven_moved"].sum()) if not trades.empty else 0
            final_tp_hits = int(trades["final_tp_hit"].sum()) if not trades.empty else 0
            momentum_failures = int(trades["momentum_failure_exit"].sum()) if not trades.empty else 0
            rows.append(
                {
                    "scenario_key": result.key,
                    "scenario_label": result.label,
                    "tp1_hit_rate": (tp1_hits / max(len(trades), 1)) * 100.0,
                    "final_tp_hit_rate": (final_tp_hits / max(len(trades), 1)) * 100.0,
                    "breakeven_move_rate": (breakeven_moves / max(len(trades), 1)) * 100.0,
                    "breakeven_after_tp1_rate": (breakeven_moves / max(tp1_hits, 1)) * 100.0,
                    "momentum_failure_exit_rate": (momentum_failures / max(len(trades), 1)) * 100.0,
                    "average_mfe": float(trades["mfe"].mean()) if not trades.empty else 0.0,
                    "average_mae": float(trades["mae"].mean()) if not trades.empty else 0.0,
                    "average_mfe_r": float(trades["mfe_r"].mean()) if not trades.empty else 0.0,
                    "average_mae_r": float(trades["mae_r"].mean()) if not trades.empty else 0.0,
                    "average_duration_minutes": float(trades["duration_seconds"].mean() / 60.0) if not trades.empty else 0.0,
                }
            )
        return pd.DataFrame(rows).sort_values("scenario_label").reset_index(drop=True)

    def _blocked_reason_breakdown(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            if signals.empty:
                continue
            grouped = signals.loc[signals["blocked_reason"] != ""].groupby("blocked_reason", dropna=False).size().reset_index(name="blocked_count")
            for row in grouped.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "blocked_reason": row["blocked_reason"],
                        "blocked_count": int(row["blocked_count"]),
                        "blocked_rate_pct": (int(row["blocked_count"]) / max(len(signals), 1)) * 100.0,
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "blocked_count", "blocked_reason"], ascending=[True, False, True]).reset_index(drop=True)

    def _executed_rate_by_strategy(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            if signals.empty:
                continue
            grouped = signals.groupby("strategy_name", dropna=False)
            for strategy_name, group in grouped:
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "strategy_name": strategy_name,
                        "signals": int(len(group)),
                        "executed_signals": int(group["executed"].sum()),
                        "deferred_signals": int(group["deferred"].sum()),
                        "blocked_signals": int(group["final_blocked"].sum()),
                        "executed_rate_pct": (float(group["executed"].sum()) / max(len(group), 1)) * 100.0,
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "strategy_name"]).reset_index(drop=True)

    def _score_distribution(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            if signals.empty:
                continue
            bucket_map = {
                "executed": signals.loc[signals["executed"]],
                "blocked": signals.loc[signals["final_blocked"]],
                "deferred": signals.loc[signals["deferred"]],
            }
            for bucket_name, frame in bucket_map.items():
                if frame.empty:
                    continue
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "outcome_bucket": bucket_name,
                        "count": int(len(frame)),
                        "score_mean": float(frame["entry_score"].mean()),
                        "score_median": float(frame["entry_score"].median()),
                        "score_p25": float(frame["entry_score"].quantile(0.25)),
                        "score_p75": float(frame["entry_score"].quantile(0.75)),
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "outcome_bucket"]).reset_index(drop=True)

    def _family_contribution(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            signals = scenario_frames[result.key]["signals"]
            if trades.empty and signals.empty:
                continue
            signal_grouped = (
                signals.groupby(["strategy_family", "side"], dropna=False)
                .agg(signals=("strategy_name", "size"), blocked_signals=("final_blocked", "sum"), executed_signals=("executed", "sum"))
                .reset_index()
                if not signals.empty
                else pd.DataFrame(columns=["strategy_family", "side", "signals", "blocked_signals", "executed_signals"])
            )
            trade_grouped = (
                trades.groupby(["strategy_family", "side"], dropna=False)
                .agg(
                    trades=("strategy_name", "size"),
                    net_profit=("pnl", "sum"),
                    gross_profit=("pnl", lambda items: float(items[items > 0].sum())),
                    gross_loss=("pnl", lambda items: abs(float(items[items < 0].sum()))),
                    win_rate=("pnl", _win_rate),
                    average_trade=("pnl", "mean"),
                    average_mfe_r=("mfe_r", "mean"),
                    average_mae_r=("mae_r", "mean"),
                    tp1_hit_rate=("tp1_hit", lambda items: float(pd.Series(items).mean() * 100.0)),
                    momentum_failure_exit_rate=("momentum_failure_exit", lambda items: float(pd.Series(items).mean() * 100.0)),
                )
                .reset_index()
                if not trades.empty
                else pd.DataFrame(columns=["strategy_family", "side"])
            )
            merged = signal_grouped.merge(trade_grouped, on=["strategy_family", "side"], how="outer").fillna(0.0)
            for row in merged.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "strategy_family": row.get("strategy_family") or "",
                        "side": row.get("side") or "",
                        "signals": int(row.get("signals", 0) or 0),
                        "executed_signals": int(row.get("executed_signals", 0) or 0),
                        "blocked_signals": int(row.get("blocked_signals", 0) or 0),
                        "trades": int(row.get("trades", 0) or 0),
                        "net_profit": _safe_float(row.get("net_profit")),
                        "profit_factor": _profit_factor(_safe_float(row.get("gross_profit")), _safe_float(row.get("gross_loss"))),
                        "win_rate": _safe_float(row.get("win_rate")),
                        "average_trade": _safe_float(row.get("average_trade")),
                        "average_mfe_r": _safe_float(row.get("average_mfe_r")),
                        "average_mae_r": _safe_float(row.get("average_mae_r")),
                        "tp1_hit_rate": _safe_float(row.get("tp1_hit_rate")),
                        "momentum_failure_exit_rate": _safe_float(row.get("momentum_failure_exit_rate")),
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "strategy_family", "side"]).reset_index(drop=True)

    def _family_breakdown(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
        dimension: str,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            if trades.empty or dimension not in trades.columns:
                continue
            grouped = (
                trades.groupby(["strategy_family", "side", dimension], dropna=False)
                .agg(
                    trades=("strategy_name", "size"),
                    net_profit=("pnl", "sum"),
                    win_rate=("pnl", _win_rate),
                    average_trade=("pnl", "mean"),
                    profit_r=("pnl_r", "mean"),
                )
                .reset_index()
            )
            for row in grouped.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "strategy_family": row.get("strategy_family") or "",
                        "side": row.get("side") or "",
                        dimension: row.get(dimension) or "",
                        "trades": int(row.get("trades", 0) or 0),
                        "net_profit": _safe_float(row.get("net_profit")),
                        "win_rate": _safe_float(row.get("win_rate")),
                        "average_trade": _safe_float(row.get("average_trade")),
                        "average_r": _safe_float(row.get("profit_r")),
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "strategy_family", "side", dimension]).reset_index(drop=True)

    def _daily_consistency(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            if trades.empty:
                continue
            daily = trades.groupby("close_date", dropna=False)["pnl"].sum().reset_index(name="daily_pnl")
            daily["cumulative_pnl"] = daily["daily_pnl"].cumsum()
            for row in daily.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "date": row["close_date"],
                        "daily_pnl": _safe_float(row["daily_pnl"]),
                        "cumulative_pnl": _safe_float(row["cumulative_pnl"]),
                        "positive_day": _safe_float(row["daily_pnl"]) > 0,
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "date"]).reset_index(drop=True)

    def _session_consistency(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            if trades.empty:
                continue
            grouped = (
                trades.groupby("session_name", dropna=False)
                .agg(
                    trades=("strategy_name", "size"),
                    net_profit=("pnl", "sum"),
                    win_rate=("pnl", _win_rate),
                    average_trade=("pnl", "mean"),
                )
                .reset_index()
            )
            for row in grouped.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "session_name": row.get("session_name") or "",
                        "trades": int(row.get("trades", 0) or 0),
                        "net_profit": _safe_float(row.get("net_profit")),
                        "win_rate": _safe_float(row.get("win_rate")),
                        "average_trade": _safe_float(row.get("average_trade")),
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "session_name"]).reset_index(drop=True)

    def _exit_reason_distribution(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            trades = scenario_frames[result.key]["trades"]
            if trades.empty:
                continue
            grouped = (
                trades.groupby(["strategy_family", "exit_reason_code"], dropna=False)
                .size()
                .reset_index(name="trade_count")
            )
            for row in grouped.to_dict(orient="records"):
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "strategy_family": row.get("strategy_family") or "",
                        "exit_reason_code": row.get("exit_reason_code") or "",
                        "trade_count": int(row.get("trade_count", 0) or 0),
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "strategy_family", "trade_count"], ascending=[True, True, False]).reset_index(drop=True)

    def _deferred_conversion(
        self,
        scenario_results: list[ScenarioResult],
        scenario_frames: dict[str, dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for result in scenario_results:
            signals = scenario_frames[result.key]["signals"]
            trades = scenario_frames[result.key]["trades"]
            for action in ["WAIT_RETEST", "PLACE_LIMIT"]:
                action_signals = signals.loc[signals["execution_action"] == action] if not signals.empty else pd.DataFrame()
                action_trades = trades.loc[trades["execution_action"] == action] if not trades.empty else pd.DataFrame()
                gross_profit = float(action_trades.loc[action_trades["pnl"] > 0, "pnl"].sum()) if not action_trades.empty else 0.0
                gross_loss = abs(float(action_trades.loc[action_trades["pnl"] < 0, "pnl"].sum())) if not action_trades.empty else 0.0
                rows.append(
                    {
                        "scenario_key": result.key,
                        "scenario_label": result.label,
                        "execution_action": action,
                        "signals": int(len(action_signals)),
                        "trades": int(len(action_trades)),
                        "conversion_rate_pct": (len(action_trades) / max(len(action_signals), 1)) * 100.0,
                        "win_rate": _win_rate(action_trades["pnl"]) if not action_trades.empty else 0.0,
                        "profit_factor": _profit_factor(gross_profit, gross_loss),
                        "average_trade": float(action_trades["pnl"].mean()) if not action_trades.empty else 0.0,
                    }
                )
        return pd.DataFrame(rows).sort_values(["scenario_label", "execution_action"]).reset_index(drop=True)

    @staticmethod
    def _scenario_comparison(
        portfolio: pd.DataFrame,
        signal_funnel: pd.DataFrame,
        execution_quality: pd.DataFrame,
        management_quality: pd.DataFrame,
    ) -> pd.DataFrame:
        if portfolio.empty:
            return pd.DataFrame()
        merged = (
            portfolio.merge(signal_funnel, on=["scenario_key", "scenario_label"], how="left", suffixes=("", "_signal"))
            .merge(execution_quality, on=["scenario_key", "scenario_label"], how="left", suffixes=("", "_execution"))
            .merge(management_quality, on=["scenario_key", "scenario_label"], how="left", suffixes=("", "_management"))
        )
        baseline_row = merged.loc[merged["scenario_key"] == "bot_v1_baseline"]
        if baseline_row.empty:
            return merged.sort_values("scenario_label").reset_index(drop=True)
        baseline = baseline_row.iloc[0]
        delta_columns = {
            "delta_net_profit": "net_profit",
            "delta_profit_factor": "profit_factor",
            "delta_win_rate": "win_rate",
            "delta_executed_rate_pct": "executed_rate_pct",
            "delta_block_rate_pct": "block_rate_pct",
            "delta_tp1_hit_rate": "tp1_hit_rate",
            "delta_momentum_failure_exit_rate": "momentum_failure_exit_rate",
        }
        for target, source in delta_columns.items():
            merged[target] = merged[source] - float(baseline[source])
        return merged.sort_values("scenario_label").reset_index(drop=True)

    @staticmethod
    def _quality_vs_participation(
        portfolio: pd.DataFrame,
        signal_funnel: pd.DataFrame,
        execution_quality: pd.DataFrame,
        management_quality: pd.DataFrame,
    ) -> list[dict[str, Any]]:
        if portfolio.empty:
            return []
        merged = (
            portfolio.merge(signal_funnel, on=["scenario_key", "scenario_label"], how="left")
            .merge(execution_quality, on=["scenario_key", "scenario_label"], how="left")
            .merge(management_quality, on=["scenario_key", "scenario_label"], how="left")
        )
        baseline_row = merged.loc[merged["scenario_key"] == "bot_v1_baseline"]
        if baseline_row.empty:
            return []
        baseline = baseline_row.iloc[0]
        verdicts: list[dict[str, Any]] = []
        for _, row in merged.iterrows():
            if row["scenario_key"] == "bot_v1_baseline":
                continue
            quality_score = 0
            quality_score += 1 if float(row["profit_factor"]) > float(baseline["profit_factor"]) else 0
            quality_score += 1 if float(row["win_rate"]) > float(baseline["win_rate"]) else 0
            quality_score += 1 if float(row["average_trade"]) > float(baseline["average_trade"]) else 0
            quality_score += 1 if float(row.get("tp1_hit_rate", 0.0)) > float(baseline.get("tp1_hit_rate", 0.0)) else 0
            quality_score += 1 if float(row.get("momentum_failure_exit_rate", 100.0)) < float(baseline.get("momentum_failure_exit_rate", 100.0)) else 0
            participation_delta = float(row.get("executed_signals", 0.0)) - float(baseline.get("executed_signals", 0.0))
            block_rate_delta = float(row.get("block_rate_pct", 0.0)) - float(baseline.get("block_rate_pct", 0.0))
            if float(row["net_profit"]) <= float(baseline["net_profit"]):
                verdict = "no_improvement"
            elif quality_score >= 4 and participation_delta > 0:
                verdict = "improved_quality_and_participation"
            elif quality_score >= 4:
                verdict = "quality_led_improvement"
            elif participation_delta > 0 and quality_score <= 2:
                verdict = "participation_led_improvement"
            else:
                verdict = "mixed_improvement"
            verdicts.append(
                {
                    "scenario_key": row["scenario_key"],
                    "scenario_label": row["scenario_label"],
                    "quality_score": int(quality_score),
                    "participation_delta": participation_delta,
                    "block_rate_delta_pct": block_rate_delta,
                    "verdict": verdict,
                    "interpretation": OOSEvaluationFramework._verdict_text(verdict),
                }
            )
        return verdicts

    @staticmethod
    def _verdict_text(verdict: str) -> str:
        mapping = {
            "improved_quality_and_participation": "Net profit improved while both participation and trade-quality markers strengthened.",
            "quality_led_improvement": "Improvement came primarily from better trade quality rather than materially higher participation.",
            "participation_led_improvement": "Improvement came mostly from taking more trades; quality metrics need extra scrutiny.",
            "mixed_improvement": "Some quality metrics improved and participation expanded, but the result is mixed rather than clean.",
            "no_improvement": "The scenario did not improve on the baseline net result.",
        }
        return mapping.get(verdict, verdict)

    def _write_report(self, output_dir: Path, report: dict[str, Any]) -> None:
        tables = report.get("tables", {}) if isinstance(report.get("tables"), dict) else {}
        manifest_frame = pd.DataFrame(report.get("scenario_manifest", []))
        if not manifest_frame.empty:
            manifest_frame.to_csv(output_dir / "scenario_manifest.csv", index=False)
        for name, frame in tables.items():
            if isinstance(frame, pd.DataFrame):
                frame.to_csv(output_dir / f"{name}.csv", index=False)
        save_json_atomic(
            output_dir / "report.json",
            {
                "generated_at": report.get("generated_at"),
                "request": report.get("request"),
                "scenario_manifest": report.get("scenario_manifest"),
                "quality_verdicts": report.get("quality_verdicts"),
                "tables": {
                    name: frame.to_dict(orient="records")
                    for name, frame in tables.items()
                    if isinstance(frame, pd.DataFrame)
                },
            },
        )
        summary_md = self._summary_markdown(report)
        (output_dir / "evaluation_summary.md").write_text(summary_md, encoding="utf-8")
        (output_dir / "comparison_methodology.md").write_text(self._methodology_markdown(), encoding="utf-8")
        (output_dir / "recommended_charts.md").write_text(self._recommended_charts_markdown(), encoding="utf-8")

    def _summary_markdown(self, report: dict[str, Any]) -> str:
        tables = report.get("tables", {}) if isinstance(report.get("tables"), dict) else {}
        portfolio = tables.get("portfolio_summary", pd.DataFrame())
        signal_funnel = tables.get("signal_funnel", pd.DataFrame())
        execution_quality = tables.get("execution_quality", pd.DataFrame())
        management_quality = tables.get("management_quality", pd.DataFrame())
        comparison = tables.get("scenario_comparison", pd.DataFrame())
        verdicts = pd.DataFrame(report.get("quality_verdicts", []))
        sections = [
            "# OOS Evaluation Summary",
            "",
            f"Generated at: {report.get('generated_at')}",
            "",
            "## Portfolio Summary",
            _scenario_markdown_table(
                portfolio[
                    [
                        "scenario_label",
                        "net_profit",
                        "profit_factor",
                        "win_rate",
                        "total_trades",
                        "average_trade",
                        "max_drawdown",
                        "average_daily_pnl",
                    ]
                ]
                if not portfolio.empty
                else pd.DataFrame()
            ),
            "",
            "## Signal Funnel",
            _scenario_markdown_table(
                signal_funnel[
                    [
                        "scenario_label",
                        "total_signals",
                        "qualified_signals",
                        "executed_signals",
                        "deferred_signals",
                        "blocked_signals",
                        "block_rate_pct",
                    ]
                ]
                if not signal_funnel.empty
                else pd.DataFrame()
            ),
            "",
            "## Execution Quality",
            _scenario_markdown_table(
                execution_quality[
                    [
                        "scenario_label",
                        "avg_entry_distance_from_value_atr",
                        "avg_fill_delta_price",
                        "retest_conversion_rate",
                        "limit_conversion_rate",
                        "wait_retest_avg_trade",
                        "limit_avg_trade",
                    ]
                ]
                if not execution_quality.empty
                else pd.DataFrame()
            ),
            "",
            "## Management Quality",
            _scenario_markdown_table(
                management_quality[
                    [
                        "scenario_label",
                        "tp1_hit_rate",
                        "final_tp_hit_rate",
                        "breakeven_after_tp1_rate",
                        "momentum_failure_exit_rate",
                        "average_mfe_r",
                        "average_mae_r",
                    ]
                ]
                if not management_quality.empty
                else pd.DataFrame()
            ),
            "",
            "## Scenario Comparison Vs Baseline",
            _scenario_markdown_table(
                comparison[
                    [
                        "scenario_label",
                        "delta_net_profit",
                        "delta_profit_factor",
                        "delta_win_rate",
                        "delta_executed_rate_pct",
                        "delta_block_rate_pct",
                        "delta_tp1_hit_rate",
                        "delta_momentum_failure_exit_rate",
                    ]
                ]
                if isinstance(comparison, pd.DataFrame) and not comparison.empty
                else pd.DataFrame()
            ),
            "",
            "## Quality Vs Participation",
            _scenario_markdown_table(
                verdicts[
                    [
                        "scenario_label",
                        "quality_score",
                        "participation_delta",
                        "block_rate_delta_pct",
                        "verdict",
                    ]
                ]
                if not verdicts.empty
                else pd.DataFrame()
            ),
        ]
        return "\n".join(sections).strip() + "\n"

    @staticmethod
    def _methodology_markdown() -> str:
        return """# Comparison Methodology

1. Every scenario uses the same OOS date range, symbol, timeframe, spread model, slippage model, execution model, balance, and risk unless the scenario definition explicitly changes a control required by the test matrix.
2. `Bot V1 Baseline` should ideally point to a true pre-V2 bundle or run id. If no external baseline is supplied, the framework falls back to an emulated baseline using the V2 codebase with adaptive execution and family-specific management disabled. That fallback is useful, but it is not a perfect historical V1 reconstruction.
3. `qualified_signals` means setup-valid candidates that reached the entry-evaluation stage in the current replay architecture.
4. `deferred_signals` are signals whose execution decision was `WAIT_RETEST` or `PLACE_LIMIT`.
5. `blocked_signals` are final non-executed signals with a concrete blocked or cancelled reason. `hard_blocked_signals` isolates signals routed straight to `BLOCK`.
6. Portfolio PnL is measured from closed trades. Average daily PnL is normalized across the full calendar span between run start and run end, not only active trading days.
7. `wait-retest` and `limit` conversion rates are computed as executed trades divided by signals tagged with that execution action.
8. `TP1 hit` is detected from an explicit `partial_tp1` management event and falls back to MFE reaching the stored TP1 level when needed.
9. `final TP hit` uses explicit TP-like exit codes first and then falls back to price proximity against the stored TP level.
10. Quality-vs-participation verdicts compare each scenario to the baseline using net profit, profit factor, win rate, average trade, TP1 hit rate, momentum-failure exit rate, executed-signal delta, and block-rate delta.
"""

    @staticmethod
    def _recommended_charts_markdown() -> str:
        return """# Recommended Charts

1. Portfolio comparison bars: net profit, profit factor, win rate, max drawdown, average trade by scenario.
2. Signal funnel stacked bars: total, qualified, executed, deferred, blocked by scenario.
3. Deferred conversion bars: `WAIT_RETEST` and `PLACE_LIMIT` conversion rate, win rate, and average trade.
4. Daily cumulative PnL lines: one line per scenario to compare stability and drawdown shape.
5. Family contribution heatmap: strategy family x side with net profit or profit factor.
6. Exit reason stacked bars: exit reason mix by strategy family and by scenario.
7. Entry quality boxplots: executed-trade `value_distance_atr` and `fill_delta_r` by scenario.
8. Score distribution violin or histogram: blocked vs executed signal entry-score distributions.
9. Session contribution bars: net profit and win rate by session for each scenario.
10. Regime contribution bars: net profit, trade count, and momentum-failure exit rate by regime bucket.
"""
