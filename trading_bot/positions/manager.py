from __future__ import annotations

from typing import Any

from risk_manager import RiskManager
from trading_bot.core.models import PositionState


class PositionManager:
    """Shared position management facade around existing risk management rules."""

    def __init__(self, risk_manager: RiskManager) -> None:
        self.risk_manager = risk_manager

    def evaluate(self, position: PositionState | dict[str, Any], trigger_df, setup_df, current_price: float, now_utc, symbol_spec: dict[str, Any]) -> list[dict[str, Any]]:
        payload = position if isinstance(position, dict) else {
            "direction": position.side,
            "entry_price": position.entry_price,
            "sl": position.stop_loss,
            "tp": position.take_profit,
            "volume": position.volume,
            "opened_at": position.opened_at,
            **position.metadata,
        }
        return self.risk_manager.evaluate_management_actions(payload, trigger_df, setup_df, current_price, now_utc, symbol_spec)

