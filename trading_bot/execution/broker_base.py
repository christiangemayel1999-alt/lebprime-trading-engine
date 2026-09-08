from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from trading_bot.core.models import ExecutionRequest


class BrokerBase(ABC):
    """Shared broker adapter interface for all environments."""

    mode: str

    @abstractmethod
    def submit_order(self, request: ExecutionRequest) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def modify_stop(self, position_id: str, new_stop: float, reason: str) -> dict[str, Any]:
        raise NotImplementedError

    def modify_position(
        self,
        position_id: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Modify a live/simulated position without exposing adapter internals."""
        if sl is not None and tp is None:
            return self.modify_stop(position_id, float(sl), reason)
        raise NotImplementedError

    @abstractmethod
    def partial_close(self, position_id: str, volume: float, reason: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close_position(self, position_id: str, reason: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_open_positions(self) -> list[Any]:
        return []

    def get_pending_orders(self) -> list[Any]:
        return []

    def get_account_state(self) -> dict[str, Any]:
        return {}

    def poll_events(self) -> list[dict[str, Any]]:
        return []
