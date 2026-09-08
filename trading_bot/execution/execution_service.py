from __future__ import annotations

from typing import Any

from trading_bot.core.models import ExecutionRequest
from trading_bot.execution.broker_base import BrokerBase


class ExecutionService:
    """Mode-agnostic execution facade."""

    def __init__(self, broker: BrokerBase) -> None:
        self.broker = broker

    def execute(self, request: ExecutionRequest) -> dict[str, Any]:
        return self.broker.submit_order(request)

    def submit_order(self, request: ExecutionRequest) -> dict[str, Any]:
        return self.execute(request)

    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        return self.broker.cancel_order(order_id, reason)

    def modify_position(
        self,
        position_id: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.broker.modify_position(position_id, sl=sl, tp=tp, reason=reason)

    def modify_stop(self, position_id: str, new_stop: float, reason: str) -> dict[str, Any]:
        return self.broker.modify_stop(position_id, new_stop, reason)

    def partial_close(self, position_id: str, volume: float, reason: str) -> dict[str, Any]:
        return self.broker.partial_close(position_id, volume, reason)

    def close_position(self, position_id: str, reason: str) -> dict[str, Any]:
        return self.broker.close_position(position_id, reason)

    def get_open_positions(self) -> list[Any]:
        return self.broker.get_open_positions()

    def get_pending_orders(self) -> list[Any]:
        return self.broker.get_pending_orders()

    def get_account_state(self) -> dict[str, Any]:
        return self.broker.get_account_state()

    def poll_events(self) -> list[dict[str, Any]]:
        return self.broker.poll_events()
