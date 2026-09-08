from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.families import strategy_family_for_setup


@dataclass(slots=True)
class ExposureSnapshot:
    position_id: str
    symbol: str
    side: str
    setup_family: str
    strategy_family: str
    setup_fingerprint: str
    risk_pct: float
    pending: bool = False
    anchor_time: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExposureDecision:
    allowed: bool
    reason_code: str | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ExposurePolicy:
    """Controlled multi-position policy shared by live and backtest execution."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        candidate: dict[str, Any],
        projected_risk_pct: float,
        open_exposures: list[ExposureSnapshot],
        pending_exposures: list[ExposureSnapshot] | None = None,
    ) -> ExposureDecision:
        execution_cfg = self.config.get("execution", {}) if isinstance(self.config, dict) else {}
        pending_exposures = pending_exposures or []
        exposures = list(open_exposures) + list(pending_exposures)
        allow_multi = bool(execution_cfg.get("allow_multi_position", False))
        max_positions = int(execution_cfg.get("max_concurrent_positions", 1) or 1)
        if allow_multi and max_positions < 2:
            max_positions = 2
        max_per_family = int(execution_cfg.get("max_positions_per_family", 1) or 1)
        max_total_risk = float(execution_cfg.get("max_total_open_risk_pct", 1.25) or 1.25)
        max_directional_risk = float(execution_cfg.get("max_directional_risk_pct", max_total_risk) or max_total_risk)
        allow_same_direction = bool(execution_cfg.get("allow_same_direction_multi_strategy", False))
        allow_same_family = bool(execution_cfg.get("allow_same_family_stack", False))
        family = str(candidate.get("setup_family") or "")
        strategy_family = strategy_family_for_setup(family)
        side = str(candidate.get("side") or "").upper()
        fingerprint = str(candidate.get("setup_fingerprint") or "")
        anchor_time = str(candidate.get("anchor_time") or "")

        if any(item.setup_fingerprint == fingerprint and fingerprint for item in exposures):
            return ExposureDecision(False, "blocked_correlated_impulse", "duplicate_setup_fingerprint", {"setup_fingerprint": fingerprint})

        if not allow_multi and exposures:
            return ExposureDecision(False, "blocked_position_exists", "multi_position_disabled", {"active_positions": len(exposures)})

        same_family = [item for item in exposures if item.strategy_family == strategy_family]
        if same_family and not allow_same_family:
            return ExposureDecision(False, "blocked_same_family_cap", "same_family_stack_disabled", {"strategy_family": strategy_family})
        if len(same_family) >= max_per_family:
            return ExposureDecision(False, "blocked_same_family_cap", "max_positions_per_family_reached", {"strategy_family": strategy_family, "max_positions_per_family": max_per_family})

        if len(exposures) >= max_positions:
            return ExposureDecision(False, "blocked_portfolio_risk_cap", "max_concurrent_positions_reached", {"active_positions": len(exposures), "max_concurrent_positions": max_positions})

        correlated = [
            item for item in exposures
            if item.side == side and item.anchor_time and anchor_time and item.anchor_time == anchor_time
        ]
        if correlated:
            return ExposureDecision(False, "blocked_correlated_impulse", "same_impulse_moment_blocked", {"anchor_time": anchor_time, "count": len(correlated)})

        same_direction = [item for item in exposures if item.side == side]
        if same_direction and not allow_same_direction:
            return ExposureDecision(False, "blocked_directional_risk_cap", "same_direction_multi_strategy_disabled", {"side": side})

        total_risk = sum(float(item.risk_pct or 0.0) for item in exposures) + float(projected_risk_pct or 0.0)
        directional_risk = sum(float(item.risk_pct or 0.0) for item in same_direction) + float(projected_risk_pct or 0.0)
        if total_risk > max_total_risk + 1e-9:
            return ExposureDecision(False, "blocked_portfolio_risk_cap", "portfolio_open_risk_cap_exceeded", {"projected_total_open_risk_pct": total_risk, "max_total_open_risk_pct": max_total_risk})
        if directional_risk > max_directional_risk + 1e-9:
            return ExposureDecision(False, "blocked_directional_risk_cap", "directional_risk_cap_exceeded", {"projected_directional_risk_pct": directional_risk, "max_directional_risk_pct": max_directional_risk, "side": side})

        return ExposureDecision(
            True,
            None,
            "exposure_policy_pass",
            {
                "projected_total_open_risk_pct": total_risk,
                "projected_directional_risk_pct": directional_risk,
                "active_positions": len(exposures),
                "strategy_family": strategy_family,
            },
        )
