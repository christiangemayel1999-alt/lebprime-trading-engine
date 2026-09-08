from __future__ import annotations

from typing import Any

from risk_manager import RiskManager
from services.execution_flow import resolve_spread_limit
from trading_bot.core.models import RiskDecision


class RiskEngine:
    """Authoritative risk gate facade for all runtime modes.

    The existing `RiskManager` remains the sizing/management implementation;
    this facade gives callers one structured approval object before execution.
    """

    def __init__(self, config: dict[str, Any], risk_manager: RiskManager | None = None, logger: Any | None = None) -> None:
        self.config = config
        self.risk_manager = risk_manager or RiskManager(config, logger=logger)
        self.logger = logger

    def approve_order(
        self,
        *,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        market_context: dict[str, Any] | None,
        account_info: Any,
        symbol_spec: dict[str, Any],
        spread_points: float,
        live_requested: bool,
        reduced_risk: bool = False,
    ) -> RiskDecision:
        setup_name = str(candidate.get("setup_family") or "")
        regime_name = str(candidate.get("regime_name") or "")
        spread_limit, spread_source = resolve_spread_limit(
            execution_cfg=self.config.get("execution", {}),
            setup_name=setup_name,
            regime_name=regime_name,
            fallback_limit=float(self.config.get("execution", {}).get("max_spread_points", 25.0)),
        )
        if float(spread_points) > spread_limit:
            return RiskDecision(
                approved=False,
                reason_code="spread_too_wide",
                metadata={"spread_points": float(spread_points), "spread_limit": spread_limit, "spread_source": spread_source},
            )

        entry_price = float(entry.get("pending_entry_price") or entry.get("entry_price") or 0.0)
        if entry_price <= 0:
            return RiskDecision(approved=False, reason_code="invalid_entry_price")

        trade_plan = self.risk_manager.calculate_trade_levels(candidate, entry_price, symbol_spec, entry, market_context)
        quality_bucket, risk_adjustment = self._risk_adjustment(candidate, entry)
        raw_sizing = self.risk_manager.calculate_position_size(
            account_info=account_info,
            trade_plan=trade_plan,
            symbol_spec=symbol_spec,
            live_requested=live_requested,
            reduced_risk=reduced_risk,
            entry_mode=str(entry.get("entry_mode") or ""),
            risk_adjustment=1.0,
        )
        sizing = self.risk_manager.calculate_position_size(
            account_info=account_info,
            trade_plan=trade_plan,
            symbol_spec=symbol_spec,
            live_requested=live_requested,
            reduced_risk=reduced_risk,
            entry_mode=str(entry.get("entry_mode") or ""),
            risk_adjustment=risk_adjustment,
        )
        sizing = self._resized_sizing(
            requested=raw_sizing,
            adjusted=sizing,
            symbol_spec=symbol_spec,
            risk_adjustment=risk_adjustment,
        )
        if not bool(sizing.get("valid")):
            return RiskDecision(
                approved=False,
                reason_code=str(sizing.get("reason_code") or "risk_rejected"),
                metadata={
                    "trade_plan": trade_plan,
                    "sizing": sizing,
                    "raw_sizing": raw_sizing,
                    "quality_bucket": quality_bucket,
                    "risk_adjustment": risk_adjustment,
                },
            )
        reason_code = (
            "risk_approved_resized"
            if bool(sizing.get("resized_before_reject")) or abs(float(sizing.get("volume", 0.0) or 0.0) - float(raw_sizing.get("volume", 0.0) or 0.0)) > 1e-9
            else "risk_approved"
        )
        return RiskDecision(
            approved=True,
            reason_code=reason_code,
            volume=float(sizing["volume"]),
            metadata={
                "trade_plan": trade_plan,
                "sizing": sizing,
                "raw_sizing": raw_sizing,
                "spread_limit": spread_limit,
                "spread_source": spread_source,
                "quality_bucket": quality_bucket,
                "risk_adjustment": risk_adjustment,
            },
        )

    def _risk_adjustment(self, candidate: dict[str, Any], entry: dict[str, Any]) -> tuple[str, float]:
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        score = float(
            entry.get("entry_score")
            or candidate.get("candidate_quality_score")
            or candidate.get("setup_score")
            or 0.0
        )
        buckets = risk_cfg.get("quality_buckets", {}) if isinstance(risk_cfg.get("quality_buckets"), dict) else {}
        if score >= float(buckets.get("aplus_min_score", 82.0) or 82.0):
            bucket = "A+"
            adjustment = float(buckets.get("aplus_risk_multiplier", 1.0) or 1.0)
        elif score >= float(buckets.get("a_min_score", 70.0) or 70.0):
            bucket = "A"
            adjustment = float(buckets.get("a_risk_multiplier", 0.9) or 0.9)
        elif score >= float(buckets.get("b_min_score", 58.0) or 58.0):
            bucket = "B"
            adjustment = float(buckets.get("b_risk_multiplier", 0.65) or 0.65)
        else:
            bucket = "C"
            adjustment = float(buckets.get("c_risk_multiplier", 0.0) or 0.0)
        side = str(candidate.get("side") or "").upper()
        if side == "SHORT":
            adjustment *= float(risk_cfg.get("short_risk_multiplier", 0.8) or 0.8)
        family_multipliers = risk_cfg.get("family_risk_multipliers", {}) if isinstance(risk_cfg.get("family_risk_multipliers"), dict) else {}
        family_key = str(candidate.get("setup_family") or "").lower()
        if family_key in family_multipliers:
            adjustment *= float(family_multipliers.get(family_key, 1.0) or 1.0)
        return bucket, max(0.0, adjustment)

    def _resized_sizing(
        self,
        *,
        requested: dict[str, Any],
        adjusted: dict[str, Any],
        symbol_spec: dict[str, Any],
        risk_adjustment: float,
    ) -> dict[str, Any]:
        if bool(adjusted.get("valid")):
            adjusted["final_requested_volume"] = float(requested.get("requested_volume", requested.get("volume", 0.0)) or 0.0)
            adjusted["adjusted_volume"] = float(adjusted.get("volume", 0.0) or 0.0)
            adjusted["raw_requested_volume"] = float(requested.get("volume", requested.get("requested_volume", 0.0)) or 0.0)
            return adjusted
        if not bool(requested.get("valid")):
            return adjusted
        if str(adjusted.get("reason_code") or "") != "volume_below_min":
            return adjusted
        if risk_adjustment <= 0:
            return adjusted
        min_volume = max(float(symbol_spec.get("volume_min", 0.0) or 0.0), float(requested.get("min_volume", 0.0) or 0.0))
        requested_volume = float(requested.get("volume", requested.get("requested_volume", 0.0)) or 0.0)
        if requested_volume + 1e-12 < min_volume:
            return adjusted
        rescued = dict(adjusted)
        rescued.update(
            {
                "valid": True,
                "volume": min_volume,
                "normalized_volume": min_volume,
                "adjusted_volume": min_volume,
                "raw_requested_volume": requested_volume,
                "final_requested_volume": float(requested.get("requested_volume", requested_volume) or requested_volume),
                "reason": "Approved at minimum tradable size after quality/risk reduction",
                "reason_code": "risk_resized_to_min_volume",
                "adjusted": True,
                "resized_before_reject": True,
            }
        )
        return rescued
