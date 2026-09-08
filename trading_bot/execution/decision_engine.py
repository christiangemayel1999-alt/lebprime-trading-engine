from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.families import (
    STRATEGY_FAMILY_BREAKOUT,
    STRATEGY_FAMILY_COMPRESS,
    STRATEGY_FAMILY_LEBPRIM,
    quality_tier,
    strategy_family_for_setup,
    volatility_state,
)


EXECUTE_NOW = "EXECUTE_NOW"
WAIT_RETEST = "WAIT_RETEST"
PLACE_LIMIT = "PLACE_LIMIT"
BLOCK = "BLOCK"

_SOFT_REASONS = {
    "blocked_chasing_entry",
    "blocked_far_from_value_zone",
    "blocked_expanded_trigger",
    "chasing_entry",
    "entry_drift_too_far",
}
_HARD_REASONS = {
    "blocked_stale_setup",
    "blocked_stale_scalp",
    "trigger_not_confirmed",
    "trigger_score_below_threshold",
    "entry_score_below_min",
    "entry_score_below_min_score_to_trade",
    "trend_score_below_threshold",
    "setup_score_below_threshold",
    "trend_alignment_required",
    "no_momentum_candle",
    "blocked_overextended_from_value",
    "blocked_bad_session",
    "blocked_bad_regime",
    "blocked_regime_mismatch",
    "blocked_session_mismatch",
    "stop_too_wide_points",
    "stop_too_wide_atr",
    "noisy_trigger_candle",
    "oversized_trigger_candle",
    "exhaustion_trigger_candle",
    "weak_short_bias",
    "weak_short_slope",
    "short_reward_constrained",
    "short_too_extended_from_origin",
    "trigger_data_unavailable",
    "no_setup_candidate",
}


class EntryExecutionPolicy:
    """Context-aware signal-to-fill policy shared by live and backtest."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def plan(
        self,
        *,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        market_context: dict[str, Any] | None,
        family: str,
        tier: str,
    ) -> dict[str, Any] | None:
        execution_cfg = self._execution_cfg()
        if not bool(execution_cfg.get("enable_contextual_entry_mode", True)):
            return None

        market = market_context if isinstance(market_context, dict) else {}
        spread_points = float(market.get("spread_points", 0.0) or 0.0)
        max_spread_points = float(execution_cfg.get("max_spread_points", 25.0) or 25.0)
        spread_ratio = spread_points / max(max_spread_points, 1e-9)
        entry_score = float(entry.get("entry_score", 0.0) or 0.0)
        trigger_score = float(entry.get("trigger_score", 0.0) or 0.0)
        regime_score = float(candidate.get("regime_confidence", 0.0) or 0.0)
        session_score = float(candidate.get("session_quality_score", 0.0) or 0.0)
        trend_score = float(entry.get("trend_score") or candidate.get("trend_score") or 0.0)
        quality_score = float(candidate.get("candidate_quality_score", entry_score) or entry_score)
        value_distance_atr = float(entry.get("value_distance_atr") or 0.0)
        trigger_candle_atr = float(entry.get("trigger_candle_atr") or 0.0)
        drift_metrics = candidate.get("quality_metrics", {}) if isinstance(candidate.get("quality_metrics"), dict) else {}
        entry_drift_atr = float(drift_metrics.get("entry_drift_atr", value_distance_atr) or value_distance_atr)
        blocked_reasons = {str(item) for item in entry.get("blocked_reasons", []) if str(item)}
        continuation_strength = (
            (entry_score * 0.35)
            + (trigger_score * 0.20)
            + (trend_score * 0.20)
            + (regime_score * 0.15)
            + (session_score * 0.10)
        ) / 100.0
        intended_entry = self._float(entry.get("pending_entry_price") or candidate.get("intended_entry_price") or candidate.get("value_price"))
        market_entry = self._float(entry.get("entry_price"))
        aggressive_limit_price = None
        if intended_entry is not None and market_entry is not None:
            aggressive_limit_price = intended_entry + ((market_entry - intended_entry) * 0.35)

        if blocked_reasons & {"entry_drift_too_far", "chasing_entry"}:
            return {
                "mode": "skip",
                "reason_code": "order_missed_drift",
                "reason": ",".join(sorted(blocked_reasons & {"entry_drift_too_far", "chasing_entry"})),
            }

        if tier == "high" and continuation_strength >= 0.74 and spread_ratio <= 0.72 and entry_drift_atr <= float(execution_cfg.get("market_entry_max_drift_atr", 0.22) or 0.22):
            return {
                "mode": "market",
                "reason_code": "contextual_market_entry",
                "reason": f"continuation_strength={continuation_strength:.3f}",
                "entry_price": market_entry,
            }

        if bool(execution_cfg.get("aggressive_entry_allowed", True)) and quality_score >= float(execution_cfg.get("aggressive_limit_min_quality_score", 66.0) or 66.0):
            if intended_entry is not None and aggressive_limit_price is not None and entry_drift_atr <= float(execution_cfg.get("aggressive_limit_max_drift_atr", 0.55) or 0.55):
                return {
                    "mode": "aggressive_limit",
                    "reason_code": "contextual_aggressive_limit",
                    "reason": f"quality_score={quality_score:.2f}",
                    "entry_price": aggressive_limit_price,
                }

        if intended_entry is not None and entry_drift_atr <= float(execution_cfg.get("limit_entry_max_drift_atr", 1.10) or 1.10):
            if family == STRATEGY_FAMILY_BREAKOUT and continuation_strength >= 0.62 and value_distance_atr <= float(execution_cfg.get("breakout_wait_retest_max_atr", 1.2) or 1.2):
                return {
                    "mode": "wait_retest",
                    "reason_code": "deferred_wait_retest",
                    "reason": "contextual_wait_retest",
                    "entry_price": intended_entry,
                }
            if trigger_candle_atr <= float(execution_cfg.get("limit_trigger_max_atr", 1.55) or 1.55):
                return {
                    "mode": "limit",
                    "reason_code": "deferred_limit_entry",
                    "reason": "contextual_limit_entry",
                    "entry_price": intended_entry,
                }

        return {
            "mode": "skip",
            "reason_code": "blocked_low_fill_probability",
            "reason": f"strength={continuation_strength:.3f}|drift_atr={entry_drift_atr:.3f}|spread_ratio={spread_ratio:.3f}",
        }

    def _execution_cfg(self) -> dict[str, Any]:
        payload = self.config.get("execution", {}) if isinstance(self.config, dict) else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class ExecutionDecision:
    action: str
    reason_code: str
    reason: str
    entry_mode: str
    order_type: str
    entry_price: float | None
    reference_price: float | None
    strategy_family: str
    quality_tier: str
    management_profile: str
    pending_expiry_bars: int = 0
    pending_expiry_minutes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_deferred(self) -> bool:
        return self.action in {WAIT_RETEST, PLACE_LIMIT}


class ExecutionDecisionEngine:
    """Route a validated signal into immediate execution, deferred execution, or block."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def decide(
        self,
        *,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        market_context: dict[str, Any] | None = None,
    ) -> ExecutionDecision:
        family = strategy_family_for_setup(str(candidate.get("setup_family") or ""))
        score = float(entry.get("entry_score") or candidate.get("setup_score") or 0.0)
        regime_score = float(candidate.get("regime_confidence") or 0.0)
        session_score = float(candidate.get("session_quality_score") or 0.0)
        value_distance_atr = float(entry.get("value_distance_atr") or 0.0)
        trigger_candle_atr = float(entry.get("trigger_candle_atr") or 0.0)
        tier = quality_tier(score, self.config)
        volatility = volatility_state(market_context, candidate)
        blocked_reasons = [str(item) for item in entry.get("blocked_reasons", []) if str(item)]
        hard_reasons = [reason for reason in blocked_reasons if reason in _HARD_REASONS]
        non_soft_reasons = [reason for reason in blocked_reasons if reason not in _SOFT_REASONS]
        execution_cfg = self._execution_cfg()
        pending_cfg = execution_cfg.get("pending_policy", {}) if isinstance(execution_cfg.get("pending_policy"), dict) else {}
        entry_price = self._float_or_none(entry.get("entry_price"))
        value_price = self._float_or_none(candidate.get("value_price"))
        structure_level = self._float_or_none(candidate.get("structure_level"))
        reference_price = self._reference_price(candidate)
        contextual_policy = EntryExecutionPolicy(self.config)

        if not bool(execution_cfg.get("enable_adaptive_execution", True)) and not bool(execution_cfg.get("enable_contextual_entry_mode", True)):
            return self._legacy_passthrough(
                family=family,
                tier=tier,
                candidate=candidate,
                entry=entry,
                blocked_reasons=blocked_reasons,
                entry_price=entry_price,
                reference_price=reference_price,
                pending_cfg=pending_cfg,
                volatility=volatility,
            )

        if hard_reasons or (non_soft_reasons and not entry.get("live_ready")):
            return self._block(
                family=family,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code=hard_reasons[0] if hard_reasons else non_soft_reasons[0],
                reason=", ".join(hard_reasons or non_soft_reasons),
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        if family == STRATEGY_FAMILY_LEBPRIM:
            strategy_cfg = self.config.get("strategy", {}) if isinstance(self.config.get("strategy"), dict) else {}
            if str(strategy_cfg.get("lebprim_mode", "restricted") or "restricted").lower() == "off":
                return self._block(
                    family=family,
                    tier=tier,
                    entry_price=entry_price,
                    reference_price=reference_price,
                    reason_code="blocked_lebprim_disabled",
                    reason="lebprim_mode_off",
                    entry=entry,
                    candidate=candidate,
                    volatility=volatility,
                )

        policy_plan = contextual_policy.plan(
            candidate=candidate,
            entry=entry,
            market_context=market_context,
            family=family,
            tier=tier,
        )
        if policy_plan is not None:
            mode = str(policy_plan.get("mode") or "")
            if mode == "market":
                return self._execute_now(
                    family=family,
                    tier=tier,
                    entry={**entry, "entry_mode": "contextual_market", "entry_price": policy_plan.get("entry_price") or entry.get("entry_price")},
                    candidate=candidate,
                    volatility=volatility,
                )
            if mode == "aggressive_limit":
                return self._defer(
                    action=PLACE_LIMIT,
                    reason_code=str(policy_plan.get("reason_code") or "contextual_aggressive_limit"),
                    reason=str(policy_plan.get("reason") or "contextual_aggressive_limit"),
                    family=family,
                    tier=tier,
                    entry={**entry, "entry_mode": "aggressive_limit"},
                    candidate=candidate,
                    entry_price=self._float_or_none(policy_plan.get("entry_price")),
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )
            if mode == "limit":
                return self._defer(
                    action=PLACE_LIMIT,
                    reason_code=str(policy_plan.get("reason_code") or "deferred_limit_entry"),
                    reason=str(policy_plan.get("reason") or "contextual_limit_entry"),
                    family=family,
                    tier=tier,
                    entry={**entry, "entry_mode": "limit_value"},
                    candidate=candidate,
                    entry_price=self._float_or_none(policy_plan.get("entry_price")),
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )
            if mode == "wait_retest":
                return self._defer(
                    action=WAIT_RETEST,
                    reason_code=str(policy_plan.get("reason_code") or "deferred_wait_retest"),
                    reason=str(policy_plan.get("reason") or "contextual_wait_retest"),
                    family=family,
                    tier=tier,
                    entry={**entry, "entry_mode": "wait_retest"},
                    candidate=candidate,
                    entry_price=self._float_or_none(policy_plan.get("entry_price")) or reference_price,
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )
            if mode == "skip":
                return self._block(
                    family=family,
                    tier=tier,
                    entry_price=entry_price,
                    reference_price=reference_price,
                    reason_code=str(policy_plan.get("reason_code") or "blocked_low_fill_probability"),
                    reason=str(policy_plan.get("reason") or "blocked_low_fill_probability"),
                    entry=entry,
                    candidate=candidate,
                    volatility=volatility,
                )

        if family == STRATEGY_FAMILY_LEBPRIM:
            return self._decide_lebprim(
                candidate=candidate,
                entry=entry,
                blocked_reasons=blocked_reasons,
                score=score,
                regime_score=regime_score,
                session_score=session_score,
                value_distance_atr=value_distance_atr,
                tier=tier,
                volatility=volatility,
                pending_cfg=pending_cfg,
            )

        if bool(entry.get("live_ready")) and not blocked_reasons:
            return self._execute_now(
                family=family,
                tier=tier,
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        if value_distance_atr >= float(execution_cfg.get("hard_extension_atr", 1.8) or 1.8):
            return self._block(
                family=family,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code="blocked_chasing_entry_hard",
                reason=f"extended_atr={value_distance_atr:.3f}",
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        if family == STRATEGY_FAMILY_BREAKOUT:
            if tier == "high" and regime_score >= 65.0 and value_distance_atr <= float(execution_cfg.get("breakout_wait_retest_max_atr", 1.2) or 1.2):
                retest_price = structure_level if structure_level is not None else value_price
                return self._defer(
                    action=WAIT_RETEST,
                    reason_code="deferred_wait_retest",
                    reason="extended_breakout_wait_retest",
                    family=family,
                    tier=tier,
                    entry=entry,
                    candidate=candidate,
                    entry_price=retest_price,
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )
            if tier in {"high", "medium"} and value_price is not None:
                return self._defer(
                    action=PLACE_LIMIT,
                    reason_code="deferred_limit_entry",
                    reason="breakout_limit_reprice",
                    family=family,
                    tier=tier,
                    entry=entry,
                    candidate=candidate,
                    entry_price=value_price,
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )

        if family == STRATEGY_FAMILY_COMPRESS:
            if tier in {"high", "medium"} and value_price is not None and trigger_candle_atr <= float(execution_cfg.get("compress_limit_trigger_atr", 1.45) or 1.45):
                return self._defer(
                    action=PLACE_LIMIT,
                    reason_code="deferred_limit_entry",
                    reason="compression_release_limit_entry",
                    family=family,
                    tier=tier,
                    entry=entry,
                    candidate=candidate,
                    entry_price=value_price,
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )
            if tier == "high" and value_distance_atr <= float(execution_cfg.get("compress_wait_retest_max_atr", 1.35) or 1.35):
                return self._defer(
                    action=WAIT_RETEST,
                    reason_code="deferred_wait_retest",
                    reason="compression_release_wait_retest",
                    family=family,
                    tier=tier,
                    entry=entry,
                    candidate=candidate,
                    entry_price=value_price if value_price is not None else reference_price,
                    reference_price=reference_price,
                    pending_cfg=pending_cfg,
                    volatility=volatility,
                )

        fallback_reason = blocked_reasons[0] if blocked_reasons else "blocked_regime_mismatch"
        return self._block(
            family=family,
            tier=tier,
            entry_price=entry_price,
            reference_price=reference_price,
            reason_code=fallback_reason if fallback_reason not in _SOFT_REASONS else "blocked_low_quality_retest",
            reason=", ".join(blocked_reasons) if blocked_reasons else "execution_router_block",
            entry=entry,
            candidate=candidate,
            volatility=volatility,
        )

    def _legacy_passthrough(
        self,
        *,
        family: str,
        tier: str,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        blocked_reasons: list[str],
        entry_price: float | None,
        reference_price: float | None,
        pending_cfg: dict[str, Any],
        volatility: str,
    ) -> ExecutionDecision:
        strategy_cfg = self.config.get("strategy", {}) if isinstance(self.config.get("strategy"), dict) else {}
        if family == STRATEGY_FAMILY_LEBPRIM and str(strategy_cfg.get("lebprim_mode", "restricted") or "restricted").lower() == "off":
            return self._block(
                family=family,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code="blocked_lebprim_disabled",
                reason="lebprim_mode_off",
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        legacy_reason_code = str(entry.get("reason_code") or (blocked_reasons[0] if blocked_reasons else "entry_not_ready"))
        legacy_reason = str(entry.get("reason") or ", ".join(blocked_reasons) or legacy_reason_code)
        if blocked_reasons or not bool(entry.get("live_ready", entry.get("valid", False))):
            return self._block(
                family=family,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code=legacy_reason_code,
                reason=legacy_reason,
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        entry_mode = str(entry.get("entry_mode") or "")
        if entry_mode in {"lebprim_limit", "limit_value", "wait_retest"}:
            return self._defer(
                action=PLACE_LIMIT,
                reason_code="legacy_pending_entry",
                reason="legacy_passthrough_pending_entry",
                family=family,
                tier=tier,
                entry=entry,
                candidate=candidate,
                entry_price=self._float_or_none(entry.get("pending_entry_price") or entry.get("entry_price")),
                reference_price=reference_price,
                pending_cfg=pending_cfg,
                volatility=volatility,
            )

        return self._execute_now(
            family=family,
            tier=tier,
            entry=entry,
            candidate=candidate,
            volatility=volatility,
        )

    def _decide_lebprim(
        self,
        *,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        blocked_reasons: list[str],
        score: float,
        regime_score: float,
        session_score: float,
        value_distance_atr: float,
        tier: str,
        volatility: str,
        pending_cfg: dict[str, Any],
    ) -> ExecutionDecision:
        strategy_cfg = self.config.get("strategy", {}) if isinstance(self.config.get("strategy"), dict) else {}
        mode = str(strategy_cfg.get("lebprim_mode", "restricted") or "restricted").lower()
        entry_price = self._float_or_none(entry.get("pending_entry_price") or entry.get("entry_price"))
        reference_price = self._reference_price(candidate)
        if mode == "off":
            return self._block(
                family=STRATEGY_FAMILY_LEBPRIM,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code="blocked_lebprim_disabled",
                reason="lebprim_mode_off",
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )

        restricted_score = float(strategy_cfg.get("lebprim_restricted_min_entry_score", 60.0) or 60.0)
        research_score = float(strategy_cfg.get("lebprim_research_min_entry_score", 52.0) or 52.0)
        min_score = restricted_score if mode == "restricted" else research_score
        min_regime = float(strategy_cfg.get("lebprim_min_regime_confidence", 62.0) or 62.0)
        min_session = float(strategy_cfg.get("lebprim_min_session_quality", 80.0 if mode == "restricted" else 68.0) or 68.0)
        if blocked_reasons:
            return self._block(
                family=STRATEGY_FAMILY_LEBPRIM,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code="blocked_lebprim_research_filter" if mode == "research" else "blocked_lebprim_restricted_filter",
                reason=", ".join(blocked_reasons),
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )
        if score < min_score or regime_score < min_regime or session_score < min_session or value_distance_atr > 1.65:
            return self._block(
                family=STRATEGY_FAMILY_LEBPRIM,
                tier=tier,
                entry_price=entry_price,
                reference_price=reference_price,
                reason_code="blocked_lebprim_research_filter" if mode == "research" else "blocked_lebprim_restricted_filter",
                reason=f"score={score:.2f}|regime={regime_score:.2f}|session={session_score:.2f}|value_atr={value_distance_atr:.3f}",
                entry=entry,
                candidate=candidate,
                volatility=volatility,
            )
        return self._defer(
            action=PLACE_LIMIT,
            reason_code="deferred_limit_entry",
            reason=f"lebprim_{mode}_value_retest",
            family=STRATEGY_FAMILY_LEBPRIM,
            tier=tier,
            entry=entry,
            candidate=candidate,
            entry_price=entry_price,
            reference_price=reference_price,
            pending_cfg=pending_cfg,
            volatility=volatility,
        )

    def _execute_now(
        self,
        *,
        family: str,
        tier: str,
        entry: dict[str, Any],
        candidate: dict[str, Any],
        volatility: str,
    ) -> ExecutionDecision:
        price = self._float_or_none(entry.get("entry_price"))
        metadata = self._metadata(
            action=EXECUTE_NOW,
            family=family,
            tier=tier,
            entry=entry,
            candidate=candidate,
            volatility=volatility,
            reason_code="execute_now",
        )
        return ExecutionDecision(
            action=EXECUTE_NOW,
            reason_code="execute_now",
            reason="entry_valid_execute_now",
            entry_mode=str(entry.get("entry_mode") or "confirmed"),
            order_type="market",
            entry_price=price,
            reference_price=self._reference_price(candidate),
            strategy_family=family,
            quality_tier=tier,
            management_profile=metadata["management_profile"],
            metadata=metadata,
        )

    def _defer(
        self,
        *,
        action: str,
        reason_code: str,
        reason: str,
        family: str,
        tier: str,
        entry: dict[str, Any],
        candidate: dict[str, Any],
        entry_price: float | None,
        reference_price: float | None,
        pending_cfg: dict[str, Any],
        volatility: str,
    ) -> ExecutionDecision:
        expiry_minutes = self._pending_expiry_minutes(action, family, tier, pending_cfg)
        expiry_bars = self._pending_expiry_bars(action, family, tier, pending_cfg)
        metadata = self._metadata(
            action=action,
            family=family,
            tier=tier,
            entry=entry,
            candidate=candidate,
            volatility=volatility,
            reason_code=reason_code,
        )
        metadata.update(
            {
                "deferred_reason": reason,
                "pending_expiry_minutes": expiry_minutes,
                "pending_expiry_bars": expiry_bars,
            }
        )
        return ExecutionDecision(
            action=action,
            reason_code=reason_code,
            reason=reason,
            entry_mode="wait_retest" if action == WAIT_RETEST else "limit_value",
            order_type="limit",
            entry_price=entry_price,
            reference_price=reference_price,
            strategy_family=family,
            quality_tier=tier,
            management_profile=metadata["management_profile"],
            pending_expiry_bars=expiry_bars,
            pending_expiry_minutes=expiry_minutes,
            metadata=metadata,
        )

    def _block(
        self,
        *,
        family: str,
        tier: str,
        entry_price: float | None,
        reference_price: float | None,
        reason_code: str,
        reason: str,
        entry: dict[str, Any],
        candidate: dict[str, Any],
        volatility: str,
    ) -> ExecutionDecision:
        metadata = self._metadata(
            action=BLOCK,
            family=family,
            tier=tier,
            entry=entry,
            candidate=candidate,
            volatility=volatility,
            reason_code=reason_code,
        )
        metadata["block_reason"] = reason
        return ExecutionDecision(
            action=BLOCK,
            reason_code=reason_code,
            reason=reason,
            entry_mode="observation_only",
            order_type="none",
            entry_price=entry_price,
            reference_price=reference_price,
            strategy_family=family,
            quality_tier=tier,
            management_profile=metadata["management_profile"],
            metadata=metadata,
        )

    def _metadata(
        self,
        *,
        action: str,
        family: str,
        tier: str,
        entry: dict[str, Any],
        candidate: dict[str, Any],
        volatility: str,
        reason_code: str,
    ) -> dict[str, Any]:
        return {
            "execution_decision": action,
            "execution_reason_code": reason_code,
            "strategy_family": family,
            "management_profile": {
                STRATEGY_FAMILY_BREAKOUT: "BreakoutManagementProfile",
                STRATEGY_FAMILY_COMPRESS: "CompressManagementProfile",
                STRATEGY_FAMILY_LEBPRIM: "LebprimManagementProfile",
            }.get(family, "TradeManagementProfile"),
            "quality_tier": tier,
            "signal_score": float(entry.get("entry_score") or candidate.get("setup_score") or 0.0),
            "value_distance_atr": float(entry.get("value_distance_atr") or 0.0),
            "trigger_candle_atr": float(entry.get("trigger_candle_atr") or 0.0),
            "regime_score": float(candidate.get("regime_confidence") or 0.0),
            "session_score": float(candidate.get("session_quality_score") or 0.0),
            "volatility_state": volatility,
            "setup_family": str(candidate.get("setup_family") or ""),
            "side": str(candidate.get("side") or ""),
        }

    def _reference_price(self, candidate: dict[str, Any]) -> float | None:
        structure = self._float_or_none(candidate.get("structure_level"))
        if structure is not None:
            return structure
        return self._float_or_none(candidate.get("value_price"))

    def _pending_expiry_minutes(self, action: str, family: str, tier: str, pending_cfg: dict[str, Any]) -> int:
        base = 5 if family == STRATEGY_FAMILY_LEBPRIM else 4
        if action == WAIT_RETEST:
            base += 1
        if tier == "high":
            base += 1
        return int(pending_cfg.get("default_expiry_minutes", base) or base)

    def _pending_expiry_bars(self, action: str, family: str, tier: str, pending_cfg: dict[str, Any]) -> int:
        base = 3 if family == STRATEGY_FAMILY_LEBPRIM else 2
        if action == WAIT_RETEST:
            base += 1
        if tier == "high":
            base += 1
        return int(pending_cfg.get("default_expiry_bars", base) or base)

    def _execution_cfg(self) -> dict[str, Any]:
        execution_cfg = self.config.get("execution", {}) if isinstance(self.config, dict) else {}
        return execution_cfg if isinstance(execution_cfg, dict) else {}

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
