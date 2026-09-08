"""Shadow testing framework for evaluating disabled strategy families."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ShadowTradeRecord:
    timestamp: datetime
    setup_family: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_score: float
    candidate_data: dict[str, Any] = field(default_factory=dict)


class ShadowStrategyRunner:
    def __init__(
        self,
        strategy_engine: Any,
        min_shadow_samples: int = 50,
        required_win_rate: float = 0.45,
        required_expectancy: float = 0.2,
        shadow_families: Optional[list[str]] = None,
    ) -> None:
        self.engine = strategy_engine
        self.min_shadow_samples = min_shadow_samples
        self.required_win_rate = required_win_rate
        self.required_expectancy = required_expectancy
        self.shadow_trades: dict[str, list[ShadowTradeRecord]] = defaultdict(list)
        self.shadow_outcomes: dict[str, list[float]] = defaultdict(list)
        self.shadow_families = shadow_families or ["TREND_PULLBACK_RECLAIM", "LIQUIDITY_SWEEP_REVERSAL"]
        self.live_family_outcomes: dict[str, list[float]] = defaultdict(list)

    def process_cycle(
        self,
        market_context: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> dict[str, list[ShadowTradeRecord]]:
        new_shadow_trades: dict[str, list[ShadowTradeRecord]] = {}
        if not hasattr(self.engine, "setup_families"):
            return new_shadow_trades

        original_config: dict[str, dict[str, Any]] = {}
        for family in self.shadow_families:
            family_key = family.lower()
            if family_key in self.engine.setup_families:
                original_config[family_key] = {
                    "enabled": self.engine.setup_families[family_key].get("enabled"),
                    "live_allowed": self.engine.setup_families[family_key].get("live_allowed"),
                }
                self.engine.setup_families[family_key]["enabled"] = True
                self.engine.setup_families[family_key]["live_allowed"] = False

        try:
            all_candidates = self.engine.generate_setup_candidates(
                market_context=market_context,
                trend_df=trend_df,
                setup_df=setup_df,
                trigger_df=trigger_df,
                symbol_spec=symbol_spec,
            )
            for candidate in all_candidates:
                family = candidate.get("setup_family")
                if family not in self.shadow_families:
                    continue
                if not candidate.get("setup_valid"):
                    continue
                entry_eval = self.engine.evaluate_entry(
                    candidate=candidate,
                    trigger_df=trigger_df,
                    symbol_spec=symbol_spec,
                    now_utc=datetime.now(),
                    live_profile=False,
                )
                if not entry_eval.get("valid"):
                    continue
                shadow_trade = ShadowTradeRecord(
                    timestamp=datetime.now(),
                    setup_family=family,
                    direction=candidate.get("side", "LONG"),
                    entry_price=float(entry_eval.get("entry_price", 0.0) or 0.0),
                    stop_loss=self._calculate_shadow_stop(candidate, entry_eval),
                    take_profit=self._calculate_shadow_tp(candidate, entry_eval),
                    entry_score=float(entry_eval.get("entry_score", 0.0) or 0.0),
                    candidate_data=candidate,
                )
                self.shadow_trades[family].append(shadow_trade)
                new_shadow_trades.setdefault(family, []).append(shadow_trade)
        finally:
            for family_key, cfg in original_config.items():
                self.engine.setup_families[family_key]["enabled"] = cfg["enabled"]
                self.engine.setup_families[family_key]["live_allowed"] = cfg["live_allowed"]
        return new_shadow_trades

    def evaluate_shadow_trade(self, trade: ShadowTradeRecord, current_price: float) -> Optional[float]:
        if trade.direction == "LONG":
            if current_price <= trade.stop_loss:
                risk = trade.entry_price - trade.stop_loss
                return (current_price - trade.entry_price) / risk if risk > 0 else -1.0
            if current_price >= trade.take_profit:
                risk = trade.entry_price - trade.stop_loss
                return (trade.take_profit - trade.entry_price) / risk if risk > 0 else 1.0
        else:
            if current_price >= trade.stop_loss:
                risk = trade.stop_loss - trade.entry_price
                return (trade.entry_price - current_price) / risk if risk > 0 else -1.0
            if current_price <= trade.take_profit:
                risk = trade.stop_loss - trade.entry_price
                return (trade.entry_price - trade.take_profit) / risk if risk > 0 else 1.0
        return None

    def update_shadow_outcomes(self, current_prices: dict[str, float]) -> None:
        for family, trades in self.shadow_trades.items():
            for trade in trades:
                current_price = current_prices.get("default", trade.entry_price)
                outcome = self.evaluate_shadow_trade(trade, current_price)
                if outcome is not None:
                    self.shadow_outcomes[family].append(outcome)

    def get_enablement_recommendation(self, family: str) -> dict[str, Any]:
        outcomes = self.shadow_outcomes.get(family, [])
        n = len(outcomes)
        if n < self.min_shadow_samples:
            return {
                "family": family,
                "recommendation": "INSUFFICIENT_DATA",
                "reason": f"Need {self.min_shadow_samples} shadow trades, have {n}",
                "metrics": {"n_shadow_trades": n, "required_samples": self.min_shadow_samples},
            }

        win_rate = sum(1 for r in outcomes if r > 0) / n
        expectancy = float(np.mean(outcomes))
        std = float(np.std(outcomes))
        sharpe = expectancy / std if std > 0 else 0.0

        if win_rate >= self.required_win_rate and expectancy >= self.required_expectancy:
            recommendation = "ENABLE"
            reason = (
                f"Meets all criteria: Win rate {win_rate:.1%} >= {self.required_win_rate:.1%}, "
                f"Expectancy {expectancy:.2f}R >= {self.required_expectancy}R"
            )
        elif win_rate < self.required_win_rate:
            recommendation = "DO_NOT_ENABLE"
            reason = f"Win rate {win_rate:.1%} below required {self.required_win_rate:.1%}"
        else:
            recommendation = "CONTINUE_SHADOW"
            reason = f"Expectancy {expectancy:.2f}R below required {self.required_expectancy}R, but improving"

        winners = [r for r in outcomes if r > 0]
        losers = [r for r in outcomes if r <= 0]
        return {
            "family": family,
            "recommendation": recommendation,
            "reason": reason,
            "metrics": {
                "n_shadow_trades": n,
                "win_rate": win_rate,
                "expectancy": expectancy,
                "sharpe_ratio": sharpe,
                "avg_winner": float(np.mean(winners)) if winners else 0.0,
                "avg_loser": float(np.mean(losers)) if losers else 0.0,
            },
        }

    def _calculate_shadow_stop(self, candidate: dict[str, Any], entry_eval: dict[str, Any]) -> float:
        entry_price = float(entry_eval.get("entry_price", 0.0) or 0.0)
        atr = float(candidate.get("atr_at_setup", 0.0) or 0.0)
        return entry_price - (atr * 1.5) if candidate.get("side") == "LONG" else entry_price + (atr * 1.5)

    def _calculate_shadow_tp(self, candidate: dict[str, Any], entry_eval: dict[str, Any]) -> float:
        entry_price = float(entry_eval.get("entry_price", 0.0) or 0.0)
        atr = float(candidate.get("atr_at_setup", 0.0) or 0.0)
        return entry_price + (atr * 2.0) if candidate.get("side") == "LONG" else entry_price - (atr * 2.0)
