from __future__ import annotations

from typing import Any

from services.simulated_executor import SimulatedExecutor
from trading_bot.core.models import ExecutionRequest
from trading_bot.execution.broker_base import BrokerBase


class ReplayBroker(BrokerBase):
    """Backtest broker adapter around the existing simulated executor."""

    mode = "BACKTEST"

    def __init__(self, executor: SimulatedExecutor) -> None:
        self.executor = executor

    def submit_order(self, request: ExecutionRequest) -> dict[str, Any]:
        if str(request.order_type).lower() == "limit":
            order = self.executor.place_pending_order(
                strategy_name=request.strategy_name,
                symbol=request.symbol,
                side=request.side,
                open_time=str(request.metadata.get("open_time") or request.metadata.get("timestamp") or ""),
                setup=str(request.metadata.get("setup") or request.metadata.get("setup_family") or ""),
                setup_fingerprint=str(request.metadata.get("setup_fingerprint") or ""),
                trigger_price=float(request.metadata.get("trigger_price") or request.entry_price),
                pending_entry_price=request.entry_price,
                volume=request.volume,
                sl=request.stop_loss,
                tp=request.take_profit,
                tp1=float(request.metadata.get("tp1") or request.take_profit),
                pending_expiry_bars=int(request.metadata.get("pending_expiry_bars") or 3),
                pending_expiry_minutes=int(request.metadata.get("pending_expiry_minutes") or 5),
                placed_bar_index=int(request.metadata.get("placed_bar_index") or 0),
                metadata=dict(request.metadata),
            )
            return {"ok": True, "status": "pending", "order_id": order.order_id}
        trade = self.executor.open_trade(
            strategy_name=request.strategy_name,
            symbol=request.symbol,
            side=request.side,
            open_time=str(request.metadata.get("open_time") or request.metadata.get("timestamp") or ""),
            entry=request.entry_price,
            sl=request.stop_loss,
            tp=request.take_profit,
            volume=request.volume,
            setup=str(request.metadata.get("setup") or request.metadata.get("setup_family") or ""),
            metadata=dict(request.metadata),
            tp1=float(request.metadata.get("tp1") or request.take_profit),
        )
        return {"ok": True, "status": "filled", "position_id": str(id(trade))}

    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        cancelled = self.executor.cancel_pending_orders(lambda order: order.order_id == order_id)
        return {"ok": bool(cancelled), "reason": reason, "cancelled": [item.order_id for item in cancelled]}

    def modify_stop(self, position_id: str, new_stop: float, reason: str) -> dict[str, Any]:
        for trade in self.executor.open_trades:
            if str(id(trade)) == str(position_id):
                return {"ok": self.executor.update_trade_stop(trade, new_stop, "", reason), "position_id": position_id}
        return {"ok": False, "reason": "position_not_found"}

    def modify_position(
        self,
        position_id: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        if sl is not None:
            result = self.modify_stop(position_id, float(sl), reason)
        else:
            result = {"ok": True, "position_id": position_id}
        if tp is not None and result.get("ok"):
            for trade in self.executor.open_trades:
                if str(id(trade)) == str(position_id):
                    trade.tp = float(tp)
                    break
        return result

    def partial_close(self, position_id: str, volume: float, reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": "partial_close_requires_bar_price", "requested_reason": reason}

    def close_position(self, position_id: str, reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": "close_requires_bar_price", "requested_reason": reason}

    def get_open_positions(self) -> list[Any]:
        return list(self.executor.open_trades)

    def get_pending_orders(self) -> list[Any]:
        return list(self.executor.pending_orders)
