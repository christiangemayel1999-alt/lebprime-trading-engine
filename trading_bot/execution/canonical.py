from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.models import ExecutionRequest


@dataclass(slots=True)
class CanonicalExecutionPlan:
    """Canonical execution plan shared by live and backtest order submission."""

    strategy_name: str
    symbol: str
    side: str
    order_type: str
    entry_price: float
    volume: float
    stop_loss: float
    take_profit: float
    setup_family: str
    setup_fingerprint: str
    entry_mode: str
    execution_model: str
    signal_time: str
    execution_time: str
    signal_bar_time: str = ""
    signal_bar_close: float | None = None
    execution_bar_time: str = ""
    executable_entry: float | None = None
    spread_used: float = 0.0
    slippage_used: float = 0.0
    fill_side: str = ""
    data_available_through_time: str = ""
    tp1: float | None = None
    trigger_price: float | None = None
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_comparable_dict(self) -> dict[str, Any]:
        """Return broker-independent execution intent for parity assertions."""
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "entry_price": float(self.entry_price),
            "volume": float(self.volume),
            "stop_loss": float(self.stop_loss),
            "take_profit": float(self.take_profit),
            "setup_family": self.setup_family,
            "setup_fingerprint": self.setup_fingerprint,
            "entry_mode": self.entry_mode,
            "execution_model": self.execution_model,
            "signal_time": self.signal_time,
            "execution_time": self.execution_time,
            "signal_bar_time": self.signal_bar_time or self.signal_time,
            "signal_bar_close": self.signal_bar_close,
            "execution_bar_time": self.execution_bar_time or self.execution_time,
            "executable_entry": self.executable_entry if self.executable_entry is not None else self.entry_price,
            "spread_used": float(self.spread_used),
            "slippage_used": float(self.slippage_used),
            "fill_side": self.fill_side,
            "data_available_through_time": self.data_available_through_time or self.signal_time,
            "tp1": self.tp1 if self.tp1 is not None else self.take_profit,
            "trigger_price": self.trigger_price if self.trigger_price is not None else self.entry_price,
            "risk_metadata": dict(self.metadata.get("risk_metadata") or {}),
            "reason_code": self.metadata.get("reason_code") or self.metadata.get("execution_reason_code"),
        }

    def to_execution_request(self, *, extra_metadata: dict[str, Any] | None = None) -> ExecutionRequest:
        merged_metadata = {
            "setup_family": self.setup_family,
            "setup": self.setup_family,
            "setup_fingerprint": self.setup_fingerprint,
            "entry_mode": self.entry_mode,
            "execution_model": self.execution_model,
            "signal_time": self.signal_time,
            "execution_time": self.execution_time,
            "signal_bar_time": self.signal_bar_time or self.signal_time,
            "signal_bar_close": self.signal_bar_close,
            "execution_bar_time": self.execution_bar_time or self.execution_time,
            "executable_entry": self.executable_entry if self.executable_entry is not None else self.entry_price,
            "spread_used": float(self.spread_used),
            "slippage_used": float(self.slippage_used),
            "fill_side": self.fill_side,
            "data_available_through_time": self.data_available_through_time or self.signal_time,
            "tp1": self.tp1 if self.tp1 is not None else self.take_profit,
            "trigger_price": self.trigger_price if self.trigger_price is not None else self.entry_price,
            **dict(self.metadata or {}),
        }
        if extra_metadata:
            merged_metadata.update(extra_metadata)
        merged_metadata.setdefault("open_time", self.execution_time)
        merged_metadata.setdefault("timestamp", self.execution_time)
        return ExecutionRequest(
            strategy_name=self.strategy_name,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            entry_price=float(self.entry_price),
            volume=float(self.volume),
            stop_loss=float(self.stop_loss),
            take_profit=float(self.take_profit),
            expires_at=self.expires_at,
            metadata=merged_metadata,
        )


def build_execution_request_from_plan(
    plan: CanonicalExecutionPlan,
    *,
    extra_metadata: dict[str, Any] | None = None,
) -> ExecutionRequest:
    """Build an ExecutionRequest from the shared canonical plan."""

    return plan.to_execution_request(extra_metadata=extra_metadata)
