from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.config.schema import ConfigSchema
from trading_bot.strategy.registry import StrategyRegistry, build_default_registry
from trading_bot.strategy.validation import CandidateQualityValidator


class UnifiedStrategyEngine:
    """Single strategy engine used by live, demo, dry-run, and backtest modes."""

    engine_type = "unified_strategy_engine"

    def __init__(self, config: dict[str, Any], base_dir: str | Path | None = None, registry: StrategyRegistry | None = None) -> None:
        self.base_dir = Path(base_dir or ".")
        self.config = ConfigSchema.normalize(config)
        self.registry = registry or build_default_registry(self.config)
        self._feature_provider = self.registry.get_for_family("LEBPRIM_SCALP") or self.registry.all()[0]
        self.validator = CandidateQualityValidator(self.config)

    def refresh_config(self, config: dict[str, Any]) -> None:
        self.config = ConfigSchema.normalize(config)
        self.validator = CandidateQualityValidator(self.config)
        for strategy in self.registry.all():
            strategy.config = self.config

    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._feature_provider.prepare_trend_dataframe(df)

    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._feature_provider.prepare_setup_dataframe(df)

    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._feature_provider.prepare_trigger_dataframe(df)

    def classify_session(self, now_utc: Any) -> dict[str, Any]:
        return self.registry.all()[0].classify_session(now_utc)

    def setup_thresholds(self, live_profile: bool) -> dict[str, float]:
        return self.registry.all()[0].setup_thresholds(live_profile)

    def analyze_market_context(self, now_utc: Any, trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, spread_points: float) -> dict[str, Any]:
        contexts: dict[str, dict[str, Any]] = {}
        primary_context: dict[str, Any] | None = None
        for strategy in self.registry.all():
            context = strategy.analyze_market_context(now_utc, trend_df, setup_df, trigger_df, spread_points)
            contexts[strategy.strategy_name] = context
            if primary_context is None:
                primary_context = dict(context)
        merged = dict(primary_context or {})
        merged["strategy_contexts"] = contexts
        return merged

    def generate_setup_candidates(self, market_context: dict[str, Any], trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, symbol_spec: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        contexts = market_context.get("strategy_contexts") if isinstance(market_context, dict) else {}
        for strategy in self.registry.all():
            context = contexts.get(strategy.strategy_name, market_context) if isinstance(contexts, dict) else market_context
            for candidate in strategy.generate_setup_candidates(context, trend_df, setup_df, trigger_df, symbol_spec):
                payload = candidate.to_legacy_dict()
                payload["raw_setup_valid"] = bool(payload.get("setup_valid"))
                payload = self._apply_candidate_validation(payload, context, trigger_df, symbol_spec)
                candidates.append(payload)
        return candidates

    def choose_best_setup(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        eligible = [candidate for candidate in candidates if candidate.get("setup_valid")]
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda item: (
                float(item.get("candidate_quality_score", item.get("setup_score", 0.0))),
                float(item.get("setup_score", 0.0)),
                float(item.get("trend_score", 0.0)),
                float(item.get("session_quality_score", 0.0)),
                float(item.get("regime_confidence", 0.0)),
            ),
            reverse=True,
        )[0]

    def evaluate_entry(self, candidate: dict[str, Any] | None, trigger_df: pd.DataFrame, symbol_spec: dict[str, Any], now_utc: Any, live_profile: bool) -> dict[str, Any]:
        if candidate is None:
            return self.registry.all()[0].evaluate_entry(candidate, trigger_df, symbol_spec, now_utc, live_profile)
        strategy = self.registry.get_for_family(str(candidate.get("setup_family") or ""))
        if strategy is None:
            strategy = self.registry.all()[0]
        return strategy.evaluate_entry(candidate, trigger_df, symbol_spec, now_utc, live_profile)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.registry.all()[0], item)

    def _apply_candidate_validation(
        self,
        candidate: dict[str, Any],
        market_context: dict[str, Any],
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> dict[str, Any]:
        latest_bar = trigger_df.iloc[-1].to_dict() if isinstance(trigger_df, pd.DataFrame) and not trigger_df.empty else None
        report = self.validator.evaluate(
            candidate=candidate,
            market_context=market_context,
            trigger_bar=latest_bar,
            symbol_spec=symbol_spec,
        )
        payload = dict(candidate)
        payload["candidate_quality_report"] = report.to_dict()
        payload["candidate_quality_score"] = float(report.score)
        payload["candidate_quality_bucket"] = str(report.quality_bucket)
        payload["quality_blocked_reasons"] = list(report.blocked_reasons)
        payload["quality_metrics"] = dict(report.metrics)
        if report.metrics.get("entry_reference_price") not in (None, ""):
            payload.setdefault("intended_entry_price", report.metrics.get("entry_reference_price"))
        if not report.valid:
            payload["invalidated_by_quality_validation"] = bool(payload.get("raw_setup_valid", payload.get("setup_valid")))
            payload["setup_valid"] = False
            payload["reason_code"] = report.blocked_reasons[0]
            payload["reason"] = ", ".join(report.blocked_reasons)
        return payload
