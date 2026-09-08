from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.families import strategy_family_for_setup


@dataclass(slots=True)
class PendingDecision:
    action: str
    reason_code: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PendingOrderPolicy:
    """Cancel degraded pending orders before they become stale or misleading."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def assess(self, pending: dict[str, Any], market_context: dict[str, Any], latest_bar: dict[str, Any] | None = None) -> PendingDecision:
        family = strategy_family_for_setup(str(pending.get("setup_family") or pending.get("setup") or ""))
        regime = market_context.get("regime") if isinstance(market_context.get("regime"), dict) else {}
        session = market_context.get("session") if isinstance(market_context.get("session"), dict) else {}
        candidate = pending.get("candidate") if isinstance(pending.get("candidate"), dict) else {}
        entry_assessment = pending.get("entry_assessment") if isinstance(pending.get("entry_assessment"), dict) else {}
        close_price = float((latest_bar or {}).get("close") or pending.get("entry_price") or 0.0)
        structure_level = self._float_or_none(candidate.get("structure_level"))
        atr_value = max(float(candidate.get("atr_at_setup") or 0.0), 1e-9)
        regime_name = str(regime.get("regime_name") or candidate.get("regime_name") or "")
        session_allowed = bool(session.get("session_live_allowed", True))
        if not session_allowed:
            return PendingDecision("cancel", "pending_session_degraded", {"regime_name": regime_name})
        if regime_name in {"NO_TRADE", "DEAD_SESSION / LOW_LIQUIDITY"}:
            return PendingDecision("cancel", "pending_regime_degraded", {"regime_name": regime_name})
        if family == "LEBPRIM":
            if float(entry_assessment.get("entry_score") or 0.0) < float(self.config.get("strategy", {}).get("lebprim_research_min_entry_score", 52.0) or 52.0):
                return PendingDecision("cancel", "pending_quality_degraded", {"entry_score": entry_assessment.get("entry_score")})
            if structure_level is not None:
                if str(pending.get("direction") or pending.get("side") or "").upper() == "LONG" and close_price < structure_level - (atr_value * 0.20):
                    return PendingDecision("cancel", "pending_structure_degraded", {"close_price": close_price, "structure_level": structure_level})
                if str(pending.get("direction") or pending.get("side") or "").upper() == "SHORT" and close_price > structure_level + (atr_value * 0.20):
                    return PendingDecision("cancel", "pending_structure_degraded", {"close_price": close_price, "structure_level": structure_level})
        return PendingDecision("keep", "pending_keep")

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
