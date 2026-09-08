from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.families import strategy_family_for_setup
from trading_bot.core.reasons import ReasonCode


@dataclass(slots=True)
class ValidationOutcome:
    valid: bool
    reason_code: str
    metrics: dict[str, Any] = field(default_factory=dict)
    score_delta: float = 0.0


@dataclass(slots=True)
class CandidateQualityReport:
    valid: bool
    blocked_reasons: list[str]
    outcomes: list[ValidationOutcome]
    score: float
    quality_bucket: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "blocked_reasons": list(self.blocked_reasons),
            "score": float(self.score),
            "quality_bucket": self.quality_bucket,
            "metrics": dict(self.metrics),
            "outcomes": [
                {
                    "valid": item.valid,
                    "reason_code": item.reason_code,
                    "metrics": dict(item.metrics),
                    "score_delta": float(item.score_delta),
                }
                for item in self.outcomes
            ],
        }


class CandidateQualityValidator:
    """Shared candidate-quality checks used before execution planning."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        candidate: dict[str, Any],
        market_context: dict[str, Any] | None,
        trigger_bar: dict[str, Any] | None,
        symbol_spec: dict[str, Any],
    ) -> CandidateQualityReport:
        outcomes: list[ValidationOutcome] = []
        metrics: dict[str, Any] = {}

        outcomes.append(self._regime_compatibility(candidate, market_context))
        outcomes.append(self._directional_bias(candidate))
        stop_outcome = self._stop_width(candidate, symbol_spec)
        outcomes.append(stop_outcome)
        metrics.update(stop_outcome.metrics)
        trigger_outcome = self._trigger_quality(candidate, trigger_bar)
        outcomes.append(trigger_outcome)
        metrics.update(trigger_outcome.metrics)
        drift_outcome = self._entry_distance(candidate, trigger_bar, symbol_spec)
        outcomes.append(drift_outcome)
        metrics.update(drift_outcome.metrics)

        blocked = [item.reason_code for item in outcomes if not item.valid and item.reason_code]
        base_score = float(candidate.get("setup_score", 0.0) or 0.0)
        quality_score = max(0.0, min(100.0, base_score + sum(float(item.score_delta) for item in outcomes)))
        metrics["candidate_quality_score"] = round(quality_score, 2)
        metrics["stop_distance_points"] = round(float(metrics.get("stop_distance_points", 0.0) or 0.0), 3)
        metrics["stop_distance_atr"] = round(float(metrics.get("stop_distance_atr", 0.0) or 0.0), 3)

        return CandidateQualityReport(
            valid=not blocked,
            blocked_reasons=blocked,
            outcomes=outcomes,
            score=round(quality_score, 2),
            quality_bucket=self._quality_bucket(quality_score),
            metrics=metrics,
        )

    def _regime_compatibility(self, candidate: dict[str, Any], market_context: dict[str, Any] | None) -> ValidationOutcome:
        market = market_context if isinstance(market_context, dict) else {}
        session = market.get("session") if isinstance(market.get("session"), dict) else {}
        regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
        family_enabled = bool(candidate.get("family_enabled", True))
        family_live_allowed = bool(candidate.get("family_live_allowed", True))
        session_allowed = bool(candidate.get("session_allowed", session.get("session_live_allowed", True)))
        regime_allowed = bool(candidate.get("regime_allowed", regime.get("live_allowed", True)))
        if not family_enabled:
            return ValidationOutcome(False, "setup_family_disabled", {"family_enabled": False}, -15.0)
        if not family_live_allowed:
            return ValidationOutcome(False, "setup_family_live_disabled", {"family_live_allowed": False}, -12.0)
        if not session_allowed:
            return ValidationOutcome(False, "blocked_bad_session", {"session_allowed": False}, -12.0)
        if not regime_allowed:
            return ValidationOutcome(False, "blocked_bad_regime", {"regime_allowed": False}, -12.0)
        return ValidationOutcome(True, "regime_compatible", {"session_allowed": True, "regime_allowed": True}, 4.0)

    def _directional_bias(self, candidate: dict[str, Any]) -> ValidationOutcome:
        side = str(candidate.get("side") or "").upper()
        bias_direction = str(candidate.get("bias_direction") or "NONE").upper()
        long_score = float(candidate.get("bias_long_score", 0.0) or 0.0)
        short_score = float(candidate.get("bias_short_score", 0.0) or 0.0)
        bias_delta = float(candidate.get("bias_delta", abs(long_score - short_score)) or 0.0)
        ema_slope_atr = float(candidate.get("ema_slope_atr", 0.0) or 0.0)
        structure_score = float(candidate.get("structure_quality_score", candidate.get("setup_score", 0.0)) or 0.0)
        profit_headroom_atr = self._float(candidate.get("profit_headroom_atr"))
        extension_atr = self._float(candidate.get("price_extension_atr"))
        metrics = {
            "bias_direction": bias_direction,
            "bias_delta": round(bias_delta, 3),
            "ema_slope_atr": round(ema_slope_atr, 3),
            "profit_headroom_atr": round(float(profit_headroom_atr or 0.0), 3),
            "price_extension_atr": round(float(extension_atr or 0.0), 3),
        }

        if side != "SHORT":
            aligned = bias_direction in {"NONE", side}
            return ValidationOutcome(aligned, "directional_bias_ok" if aligned else "trend_alignment_required", metrics, 4.0 if aligned else -10.0)

        shorts_cfg = self._shorts_cfg()
        if bool(shorts_cfg.get("require_strong_bearish_bias", True)):
            min_delta = float(shorts_cfg.get("min_bias_delta", 24.0) or 24.0)
            min_score = float(shorts_cfg.get("min_bearish_score", 64.0) or 64.0)
            if bias_direction != "SHORT" or short_score < min_score or bias_delta < min_delta:
                return ValidationOutcome(False, ReasonCode.WEAK_SHORT_BIAS.value, metrics, -18.0)
        min_slope = float(shorts_cfg.get("min_bearish_slope_atr", 0.10) or 0.10)
        if ema_slope_atr > -min_slope:
            return ValidationOutcome(False, ReasonCode.WEAK_SHORT_SLOPE.value, metrics, -16.0)
        min_structure = float(shorts_cfg.get("min_structure_score", 58.0) or 58.0)
        if structure_score < min_structure:
            return ValidationOutcome(False, "weak_short_structure", metrics, -10.0)
        min_headroom = float(shorts_cfg.get("min_reward_headroom_atr", 0.9) or 0.9)
        if profit_headroom_atr is not None and profit_headroom_atr < min_headroom:
            return ValidationOutcome(False, ReasonCode.SHORT_REWARD_CONSTRAINED.value, metrics, -12.0)
        max_extension = float(shorts_cfg.get("max_extension_from_origin_atr", 1.45) or 1.45)
        if extension_atr is not None and extension_atr > max_extension:
            return ValidationOutcome(False, "short_too_extended_from_origin", metrics, -10.0)
        return ValidationOutcome(True, "short_directional_bias_ok", metrics, 8.0)

    def _stop_width(self, candidate: dict[str, Any], symbol_spec: dict[str, Any]) -> ValidationOutcome:
        entry_reference = self._entry_reference(candidate)
        structure_level = self._float(candidate.get("structure_level"))
        atr_value = max(float(candidate.get("atr_at_setup", 0.0) or 0.0), 1e-9)
        point = max(float(symbol_spec.get("point", 0.01) or 0.01), 1e-9)
        stop_distance = abs(float(entry_reference or 0.0) - float(structure_level or 0.0))
        stop_points = stop_distance / point if stop_distance > 0 else 0.0
        stop_atr = stop_distance / atr_value if stop_distance > 0 else 0.0
        side = str(candidate.get("side") or "").upper()
        validation_cfg = self._validation_cfg()
        family_cfg = self._family_validation_cfg(candidate)
        metrics = {
            "stop_distance_price": round(stop_distance, 6),
            "stop_distance_points": round(stop_points, 3),
            "stop_distance_atr": round(stop_atr, 3),
            "entry_reference_price": entry_reference,
            "structure_level": structure_level,
        }
        if entry_reference is None or structure_level is None:
            return ValidationOutcome(False, "invalid_stop_reference", metrics, -18.0)

        max_points = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_stop_points_long",
            "max_stop_points_short",
            default=650.0,
        )
        if stop_points > max_points:
            metrics["max_stop_points"] = max_points
            return ValidationOutcome(False, ReasonCode.STOP_TOO_WIDE_POINTS.value, metrics, -24.0)

        max_atr = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_stop_atr_long",
            "max_stop_atr_short",
            default=1.8,
        )
        if stop_atr > max_atr:
            metrics["max_stop_atr"] = max_atr
            return ValidationOutcome(False, ReasonCode.STOP_TOO_WIDE_ATR.value, metrics, -20.0)

        return ValidationOutcome(True, "stop_width_ok", metrics, 6.0)

    def _trigger_quality(self, candidate: dict[str, Any], trigger_bar: dict[str, Any] | None) -> ValidationOutcome:
        if not isinstance(trigger_bar, dict):
            return ValidationOutcome(True, "trigger_bar_unavailable", {}, 0.0)
        atr_value = max(float(candidate.get("atr_at_setup", 0.0) or 0.0), 1e-9)
        side = str(candidate.get("side") or "").upper()
        validation_cfg = self._validation_cfg()
        family_cfg = self._family_validation_cfg(candidate)
        range_atr = float(trigger_bar.get("range", 0.0) or 0.0) / atr_value
        body_atr = float(trigger_bar.get("body", 0.0) or 0.0) / atr_value
        body = max(float(trigger_bar.get("body", 0.0) or 0.0), 1e-9)
        upper_wick = float(trigger_bar.get("upper_wick", 0.0) or 0.0)
        lower_wick = float(trigger_bar.get("lower_wick", 0.0) or 0.0)
        close_location = float(trigger_bar.get("close_location", 0.5) or 0.5)
        adverse_wick = upper_wick if side == "LONG" else lower_wick
        max_range = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_trigger_range_atr_long",
            "max_trigger_range_atr_short",
            default=float(validation_cfg.get("max_trigger_range_atr", 1.15) or 1.15),
        )
        max_body = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_trigger_body_atr_long",
            "max_trigger_body_atr_short",
            default=float(validation_cfg.get("max_trigger_body_atr", 0.85) or 0.85),
        )
        wick_ratio_limit = float(validation_cfg.get("max_trigger_adverse_wick_ratio", 1.15) or 1.15)
        metrics = {
            "trigger_range_atr": round(range_atr, 3),
            "trigger_body_atr": round(body_atr, 3),
            "trigger_close_location": round(close_location, 3),
            "trigger_adverse_wick_ratio": round(adverse_wick / body, 3),
        }
        if range_atr > max_range:
            metrics["max_trigger_range_atr"] = max_range
            return ValidationOutcome(False, ReasonCode.OVERSIZED_TRIGGER_CANDLE.value, metrics, -16.0)
        if body_atr > max_body:
            metrics["max_trigger_body_atr"] = max_body
            return ValidationOutcome(False, ReasonCode.NOISY_TRIGGER_CANDLE.value, metrics, -10.0)
        if (adverse_wick / body) > wick_ratio_limit and range_atr >= max(0.75, max_range * 0.75):
            return ValidationOutcome(False, ReasonCode.EXHAUSTION_TRIGGER_CANDLE.value, metrics, -12.0)
        return ValidationOutcome(True, "trigger_quality_ok", metrics, 5.0)

    def _entry_distance(self, candidate: dict[str, Any], trigger_bar: dict[str, Any] | None, symbol_spec: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(trigger_bar, dict):
            return ValidationOutcome(True, "entry_distance_unavailable", {}, 0.0)
        market_price = self._float(trigger_bar.get("close"))
        intended_entry = self._entry_reference(candidate)
        atr_value = max(float(candidate.get("atr_at_setup", 0.0) or 0.0), 1e-9)
        point = max(float(symbol_spec.get("point", 0.01) or 0.01), 1e-9)
        side = str(candidate.get("side") or "").upper()
        validation_cfg = self._validation_cfg()
        family_cfg = self._family_validation_cfg(candidate)
        drift_price = abs(float(market_price or 0.0) - float(intended_entry or 0.0))
        drift_points = drift_price / point if drift_price > 0 else 0.0
        drift_atr = drift_price / atr_value if drift_price > 0 else 0.0
        unfavorable = bool(
            market_price is not None
            and intended_entry is not None
            and (
                (side == "LONG" and market_price > intended_entry)
                or (side == "SHORT" and market_price < intended_entry)
            )
        )
        max_points = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_entry_drift_points_long",
            "max_entry_drift_points_short",
            default=float(validation_cfg.get("max_entry_drift_points", 180.0) or 180.0),
        )
        max_atr = self._side_value(
            family_cfg,
            validation_cfg,
            side,
            "max_entry_drift_atr_long",
            "max_entry_drift_atr_short",
            default=float(validation_cfg.get("max_entry_drift_atr", 0.85) or 0.85),
        )
        metrics = {
            "entry_drift_price": round(drift_price, 6),
            "entry_drift_points": round(drift_points, 3),
            "entry_drift_atr": round(drift_atr, 3),
            "market_price": market_price,
            "intended_entry_price": intended_entry,
            "unfavorable_drift": unfavorable,
        }
        if unfavorable and drift_points > max_points:
            metrics["max_entry_drift_points"] = max_points
            return ValidationOutcome(False, ReasonCode.ENTRY_DRIFT_TOO_FAR.value, metrics, -18.0)
        if unfavorable and drift_atr > max_atr:
            metrics["max_entry_drift_atr"] = max_atr
            return ValidationOutcome(False, ReasonCode.CHASING_ENTRY.value, metrics, -14.0)
        return ValidationOutcome(True, "entry_distance_ok", metrics, 3.0)

    def _quality_bucket(self, score: float) -> str:
        validation_cfg = self._validation_cfg()
        if score >= float(validation_cfg.get("quality_aplus_min_score", 82.0) or 82.0):
            return "A+"
        if score >= float(validation_cfg.get("quality_a_min_score", 70.0) or 70.0):
            return "A"
        if score >= float(validation_cfg.get("quality_b_min_score", 58.0) or 58.0):
            return "B"
        return "C"

    def _entry_reference(self, candidate: dict[str, Any]) -> float | None:
        for key in ("intended_entry_price", "value_price", "entry_price", "setup_price"):
            value = self._float(candidate.get(key))
            if value is not None:
                return value
        return None

    def _validation_cfg(self) -> dict[str, Any]:
        payload = self.config.get("validation", {}) if isinstance(self.config, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _shorts_cfg(self) -> dict[str, Any]:
        payload = self.config.get("shorts", {}) if isinstance(self.config, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _family_validation_cfg(self, candidate: dict[str, Any]) -> dict[str, Any]:
        validation_cfg = self._validation_cfg()
        families = validation_cfg.get("by_family", {}) if isinstance(validation_cfg.get("by_family"), dict) else {}
        family_key = str(strategy_family_for_setup(str(candidate.get("setup_family") or ""))).lower()
        payload = families.get(family_key, {})
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _side_value(
        family_cfg: dict[str, Any],
        root_cfg: dict[str, Any],
        side: str,
        long_key: str,
        short_key: str,
        *,
        default: float,
    ) -> float:
        key = long_key if side == "LONG" else short_key
        for payload in (family_cfg, root_cfg):
            value = payload.get(key)
            if value not in (None, ""):
                return float(value)
        return float(default)

    @staticmethod
    def _float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
