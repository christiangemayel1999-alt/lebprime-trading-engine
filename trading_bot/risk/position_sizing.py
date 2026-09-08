"""Dynamic position sizing based on market regime and signal quality."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PositionSizeConfig:
    base_risk_percent: float = 1.0
    max_risk_percent: float = 2.0
    min_risk_percent: float = 0.25
    regime_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "TREND_CONTINUATION": 1.2,
            "HIGH_VOLATILITY_BREAKOUT": 0.4,
            "RANGE_MEAN_REVERSION": 0.6,
            "NO_TRADE": 0.0,
        }
    )
    high_score_threshold: float = 70.0
    low_score_threshold: float = 52.0
    reduce_after_losses: int = 2
    loss_reduction_factor: float = 0.5


class DynamicPositionSizer:
    def __init__(self, config: PositionSizeConfig) -> None:
        self.config = config
        self.consecutive_losses = 0
        self.last_trade_profitable = True

    def calculate_position_risk(
        self,
        regime: str,
        entry_score: float,
        bias_confidence: float,
        account_balance: float,
        current_drawdown_pct: float,
    ) -> float:
        _ = account_balance
        risk_pct = self.config.base_risk_percent
        risk_pct *= self.config.regime_multipliers.get(regime, 0.5)
        risk_pct *= self._calculate_score_multiplier(entry_score)

        if bias_confidence < 0.6:
            risk_pct *= 0.5
        elif bias_confidence > 0.8:
            risk_pct *= 1.1

        if self.consecutive_losses >= self.config.reduce_after_losses:
            risk_pct *= self.config.loss_reduction_factor
            logger.info("Reducing position after %s consecutive losses", self.consecutive_losses)

        if current_drawdown_pct > 1.0:
            drawdown_factor = max(0.25, 1.0 - current_drawdown_pct / 100.0)
            risk_pct *= drawdown_factor
            logger.info("Reducing risk due to drawdown: %.1f%%", current_drawdown_pct)

        risk_pct = float(np.clip(risk_pct, self.config.min_risk_percent, self.config.max_risk_percent))
        logger.info(
            "Position risk calculated: %.2f%% (regime=%s, score=%s, confidence=%.2f)",
            risk_pct,
            regime,
            entry_score,
            bias_confidence,
        )
        return risk_pct

    def _calculate_score_multiplier(self, entry_score: float) -> float:
        if entry_score >= self.config.high_score_threshold:
            return 1.2
        if entry_score <= self.config.low_score_threshold:
            return 0.5
        score_range = self.config.high_score_threshold - self.config.low_score_threshold
        position_in_range = (entry_score - self.config.low_score_threshold) / max(score_range, 1e-9)
        return 0.5 + (position_in_range * 0.7)

    def update_after_trade(self, was_profitable: bool) -> None:
        self.consecutive_losses = 0 if was_profitable else self.consecutive_losses + 1
        self.last_trade_profitable = was_profitable
        logger.debug("Position sizer updated: consecutive_losses=%s", self.consecutive_losses)

    def get_status(self) -> dict[str, Any]:
        return {
            "consecutive_losses": self.consecutive_losses,
            "last_trade_profitable": self.last_trade_profitable,
            "is_reduced": self.consecutive_losses >= self.config.reduce_after_losses,
            "base_risk": self.config.base_risk_percent,
        }
