"""Simulated trade execution for historical replay/backtest mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.reasons import ManagementAction, ReasonCode


def _direction(side: str) -> int:
    return 1 if str(side).upper() in {"LONG", "BUY"} else -1


def _is_lebprim_setup(setup: str) -> bool:
    return str(setup).upper() == "LEBPRIM_SCALP"


@dataclass
class SimTrade:
    """Represents one simulated open trade."""

    strategy_name: str
    symbol: str
    side: str
    open_time: str
    entry: float
    sl: float
    tp: float
    tp1: float
    volume: float
    risk_amount: float
    setup: str
    metadata: dict[str, Any] = field(default_factory=dict)
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    realized_pnl: float = 0.0
    initial_volume: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    partial_closed: bool = False
    partial_closed_volume: float = 0.0
    partial_realized_pnl: float = 0.0
    breakeven_moved: bool = False
    trailing_active: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        self.initial_volume = float(self.volume)
        self.highest_price = float(self.entry)
        self.lowest_price = float(self.entry)
        self.direction = str(self.side).upper()
        self.entry_price = float(self.entry)
        self.opened_at = str(self.open_time)
        self.initial_risk_price = abs(float(self.entry) - float(self.sl))
        self.metadata.setdefault("management_events", [])
        self.metadata.setdefault("pending_order_id", None)
        self.metadata.setdefault("pending_entry_price", None)

    def position_state(self) -> dict[str, Any]:
        """Expose a mutable position state for the risk manager."""
        return self.__dict__

    @property
    def remaining_volume(self) -> float:
        return float(self.volume)

    def register_partial_close(
        self,
        close_price: float,
        partial_volume: float,
        contract_size: float,
        ts_iso: str,
        reason_code: str,
    ) -> float:
        """Apply a partial close and return realized PnL."""
        partial_volume = max(0.0, min(float(partial_volume), float(self.volume)))
        if partial_volume <= 0:
            return 0.0
        direction = _direction(self.side)
        pnl = (float(close_price) - float(self.entry)) * direction * partial_volume * float(contract_size)
        self.volume = max(0.0, float(self.volume) - partial_volume)
        self.realized_pnl += pnl
        self.partial_realized_pnl += pnl
        self.partial_closed = True
        self.partial_closed_volume += partial_volume
        self.metadata.setdefault("management_events", []).append(
            {
                "ts": ts_iso,
                "action": ManagementAction.PARTIAL_CLOSE.value,
                "reason_code": reason_code,
                "volume": partial_volume,
                "price": float(close_price),
                "pnl": pnl,
            }
        )
        return pnl

    def move_stop(self, new_sl: float, ts_iso: str, reason_code: str) -> bool:
        """Move the stop only in the profit-protecting direction."""
        if reason_code and reason_code.startswith("breakeven"):
            self.breakeven_moved = True
        if reason_code and reason_code.startswith("trailing"):
            self.trailing_active = True
        if _direction(self.side) > 0:
            if float(new_sl) <= float(self.sl):
                return False
        else:
            if float(new_sl) >= float(self.sl):
                return False
        self.sl = float(new_sl)
        self.metadata.setdefault("management_events", []).append(
            {
                "ts": ts_iso,
                "action": ManagementAction.MOVE_STOP.value,
                "reason_code": reason_code,
                "sl": float(new_sl),
            }
        )
        return True

    def close_full(
        self,
        exit_price: float,
        ts_iso: str,
        exit_reason: str,
        reason_code: str,
        contract_size: float,
        commission: float = 0.0,
        swap: float = 0.0,
    ) -> dict[str, Any]:
        """Close the remaining volume and return a trade closure payload."""
        if self.closed:
            raise RuntimeError("Trade already closed")
        remaining_volume_before_close = float(self.volume)
        self.closed = True
        direction = _direction(self.side)
        gross_final_pnl = (float(exit_price) - float(self.entry)) * direction * remaining_volume_before_close * float(contract_size)
        final_realized_pnl = gross_final_pnl - float(commission) - float(swap)
        total_pnl = float(self.realized_pnl) + final_realized_pnl
        risk_amount = max(float(self.risk_amount), 1e-9)
        pnl_r = total_pnl / risk_amount
        self.volume = 0.0
        payload = {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "side": self.side,
            "trade_id": str(self.metadata.get("trade_id") or self.metadata.get("setup_fingerprint") or self.open_time),
            "position_id": str(self.metadata.get("position_id") or self.open_time),
            "order_id": self.metadata.get("pending_order_id"),
            "setup_family": self.setup,
            "setup_fingerprint": str(self.metadata.get("setup_fingerprint") or ""),
            "open_time": self.open_time,
            "close_time": ts_iso,
            "entry": self.entry,
            "sl": self.sl,
            "tp": self.tp,
            "tp1": self.tp1,
            "exit_price": float(exit_price),
            "pnl": total_pnl,
            "pnl_r": pnl_r,
            "volume": remaining_volume_before_close,
            "volume_initial": float(self.initial_volume),
            "volume_remaining_at_close": remaining_volume_before_close,
            "realized_pnl_total": total_pnl,
            "partial_realized_pnl": float(self.partial_realized_pnl),
            "final_realized_pnl": float(final_realized_pnl),
            "commission": float(commission),
            "swap": float(swap),
            "exit_reason": exit_reason,
            "exit_reason_code": reason_code,
            "mfe": self.max_favorable,
            "mae": self.max_adverse,
            "duration_seconds": self.metadata.get("duration_seconds", 0.0),
            "remaining_volume_before_close": remaining_volume_before_close,
            "trade_metadata_json": self.metadata,
        }
        return payload


@dataclass
class PendingSimOrder:
    """Represents a limit/value-zone order waiting to be filled."""

    order_id: str
    strategy_name: str
    symbol: str
    side: str
    setup: str
    setup_fingerprint: str
    entry_mode: str
    pending_entry_price: float
    trigger_price: float
    placed_time: str
    placed_bar_index: int
    expiry_bars: int
    expiry_minutes: int
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "active"

    def is_expired(self, current_bar_index: int, ts_seconds: float) -> bool:
        if self.status != "active":
            return True
        if int(current_bar_index) - int(self.placed_bar_index) > int(self.expiry_bars):
            return True
        if float(ts_seconds) >= float(self.metadata.get("expires_at_ts", float("inf"))):
            return True
        return False


class SimulatedExecutor:
    """Simulate fills and exits with explicit spread/slippage and deterministic rules."""

    def __init__(
        self,
        spread_points: float,
        slippage_points: float,
        point: float,
        contract_size: float,
        same_bar_rule: str = "sl_first",
        allow_pyramiding: bool = False,
        commission_per_lot: float = 0.0,
    ) -> None:
        self.spread_points = float(spread_points)
        self.slippage_points = float(slippage_points)
        self.point = max(float(point), 1e-9)
        self.contract_size = max(float(contract_size), 1e-9)
        self.same_bar_rule = str(same_bar_rule or "sl_first").lower()
        self.allow_pyramiding = bool(allow_pyramiding)
        # Round-trip commission in account currency per standard lot (e.g. 7.0 = $3.50/side).
        # Applied in full at trade close (entry + exit sides combined).
        self.commission_per_lot = max(0.0, float(commission_per_lot))
        self.open_trades: list[SimTrade] = []
        self.pending_orders: list[PendingSimOrder] = []
        self._next_order_id = 1

    def update_trade_stop(self, trade: SimTrade, new_sl: float, ts_iso: str, reason_code: str) -> bool:
        """Safely tighten a trade stop without changing caller-facing interfaces."""
        return trade.move_stop(float(new_sl), ts_iso, reason_code)

    def _commission_for_trade(self, trade: SimTrade) -> float:
        """Return the round-trip commission for a trade based on its initial volume."""
        if self.commission_per_lot <= 0.0:
            return 0.0
        return float(trade.initial_volume) * self.commission_per_lot

    def close_trade_now(
        self,
        trade: SimTrade,
        exit_price: float,
        exit_reason: str,
        close_time: str,
        reason_code: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close one trade immediately and return the normal persisted payload."""
        if extra_metadata:
            trade.metadata.update(dict(extra_metadata))
        trade.metadata.setdefault("management_events", []).append(
            {
                "ts": close_time,
                "action": ManagementAction.CLOSE_FULL.value,
                "reason_code": reason_code,
                "exit_reason": exit_reason,
                "price": float(exit_price),
                "metadata": dict(extra_metadata or {}),
            }
        )
        commission = self._commission_for_trade(trade)
        payload = trade.close_full(
            exit_price=float(exit_price),
            ts_iso=close_time,
            exit_reason=exit_reason,
            reason_code=reason_code,
            contract_size=self.contract_size,
            commission=commission,
            swap=float(trade.metadata.get("swap", 0.0) or 0.0),
        )
        self.open_trades = [item for item in self.open_trades if item is not trade]
        return payload

    def has_open_position(self) -> bool:
        return bool(self.open_trades)

    def can_open(self) -> bool:
        return self.allow_pyramiding or not self.has_open_position()

    def has_conflicting_filled_position(self, symbol: str, side: str, setup: str) -> bool:
        """Return True when an active filled position should block a new signal."""
        symbol = str(symbol).upper()
        setup = str(setup).upper()
        if _is_lebprim_setup(setup):
            return any(
                str(trade.symbol).upper() == symbol and _is_lebprim_setup(str(trade.setup).upper()) and not trade.closed
                for trade in self.open_trades
            )
        return not self.allow_pyramiding and self.has_open_position()

    def has_conflicting_pending_order(self, symbol: str, side: str, setup_fingerprint: str, setup: str) -> bool:
        """Return True when an active pending order should block a duplicate signal."""
        symbol = str(symbol).upper()
        side = str(side).upper()
        fingerprint = str(setup_fingerprint or "")
        setup = str(setup).upper()
        for order in self.pending_orders:
            if order.status != "active":
                continue
            if str(order.symbol).upper() != symbol:
                continue
            if str(order.side).upper() != side:
                continue
            if _is_lebprim_setup(setup):
                if str(order.setup_fingerprint) == fingerprint:
                    return True
            elif str(order.setup).upper() == setup:
                return True
        return False

    def can_accept_signal(
        self,
        symbol: str,
        side: str,
        setup: str,
        setup_fingerprint: str | None = None,
        entry_mode: str | None = None,
    ) -> tuple[bool, str | None]:
        """Return whether a signal can be accepted without conflicting exposure."""
        setup = str(setup).upper()
        entry_mode = str(entry_mode or "").lower()
        is_limit_semantics = entry_mode in {"lebprim_limit", "limit_value", "wait_retest"} or _is_lebprim_setup(setup)
        if self.has_conflicting_filled_position(symbol, side, setup):
            return False, "position_exists"
        if is_limit_semantics and self.has_conflicting_pending_order(symbol, side, str(setup_fingerprint or ""), setup):
            return False, "pending_order_exists"
        return True, None

    def _entry_price_with_costs(self, side: str, raw_entry: float) -> float:
        spread_price = self.spread_points * self.point
        slippage_price = self.slippage_points * self.point
        if _direction(side) > 0:
            return float(raw_entry) + spread_price + slippage_price
        return float(raw_entry) - spread_price - slippage_price

    def open_trade(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        open_time: str,
        entry: float,
        sl: float,
        tp: float,
        volume: float,
        setup: str,
        metadata: dict[str, Any] | None = None,
        tp1: float | None = None,
    ) -> SimTrade:
        filled_entry = self._entry_price_with_costs(side, float(entry))
        risk_amount = abs(filled_entry - float(sl)) * float(volume) * self.contract_size
        trade = SimTrade(
            strategy_name=strategy_name,
            symbol=symbol,
            side=side,
            open_time=open_time,
            entry=filled_entry,
            sl=float(sl),
            tp=float(tp),
            tp1=float(tp1 if tp1 is not None else tp),
            volume=float(volume),
            risk_amount=max(risk_amount, 1e-9),
            setup=setup,
            metadata=metadata or {},
        )
        self.open_trades.append(trade)
        return trade

    def place_pending_order(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        open_time: str,
        setup: str,
        setup_fingerprint: str,
        trigger_price: float,
        pending_entry_price: float,
        volume: float,
        sl: float,
        tp: float,
        tp1: float,
        pending_expiry_bars: int = 3,
        pending_expiry_minutes: int = 5,
        placed_bar_index: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> PendingSimOrder:
        order_id = f"PO-{self._next_order_id:06d}"
        self._next_order_id += 1
        expires_at_ts = None
        meta = dict(metadata or {})
        placed_dt = meta.get("placed_at_ts", meta.get("created_at_ts"))
        if placed_dt is not None:
            try:
                expires_at_ts = float(placed_dt) + (float(pending_expiry_minutes) * 60.0)
            except Exception:
                expires_at_ts = None
        order = PendingSimOrder(
            order_id=order_id,
            strategy_name=strategy_name,
            symbol=symbol,
            side=side,
            setup=setup,
            setup_fingerprint=str(setup_fingerprint or ""),
            entry_mode=str(meta.get("entry_mode") or "lebprim_limit"),
            pending_entry_price=float(pending_entry_price),
            trigger_price=float(trigger_price),
            placed_time=open_time,
            placed_bar_index=int(placed_bar_index),
            expiry_bars=int(pending_expiry_bars),
            expiry_minutes=int(pending_expiry_minutes),
            metadata={
                **meta,
                "volume": float(volume),
                "sl": float(sl),
                "tp": float(tp),
                "tp1": float(tp1),
                "expires_at_ts": expires_at_ts if expires_at_ts is not None else float("inf"),
            },
        )
        self.pending_orders.append(order)
        return order

    def cancel_pending_orders(self, predicate: Any) -> list[PendingSimOrder]:
        """Cancel active pending orders matching a predicate."""
        cancelled: list[PendingSimOrder] = []
        survivors: list[PendingSimOrder] = []
        for order in self.pending_orders:
            if order.status == "active" and predicate(order):
                order.status = "cancelled"
                cancelled.append(order)
            else:
                survivors.append(order)
        self.pending_orders = survivors
        return cancelled

    def _bar_exit_price(self, trade: SimTrade, reason: str) -> float:
        if reason == "tp":
            return float(trade.tp)
        if reason == "sl":
            return float(trade.sl)
        return float(trade.entry)

    def _resolve_hit(self, trade: SimTrade, low: float, high: float) -> str | None:
        sl_hit = low <= trade.sl <= high
        tp_hit = low <= trade.tp <= high
        if not sl_hit and not tp_hit:
            return None
        if sl_hit and tp_hit:
            return "sl" if self.same_bar_rule != "tp_first" else "tp"
        return "sl" if sl_hit else "tp"

    def update_pending_orders(self, bar: dict[str, Any], ts_iso: str, bar_index: int) -> list[dict[str, Any]]:
        """Update pending limit orders and return fill/expiry events."""
        low = float(bar["low"])
        high = float(bar["high"])
        ts_seconds = 0.0
        try:
            ts_seconds = float(getattr(bar.get("time"), "timestamp", lambda: 0.0)())
        except Exception:
            ts_seconds = 0.0
        events: list[dict[str, Any]] = []
        survivors: list[PendingSimOrder] = []
        for order in self.pending_orders:
            if order.status != "active":
                continue
            if order.is_expired(bar_index, ts_seconds):
                order.status = "expired"
                events.append(
                    {
                        "event_type": "pending_expired",
                        "order_id": order.order_id,
                        "strategy_name": order.strategy_name,
                        "symbol": order.symbol,
                        "side": order.side,
                        "setup": order.setup,
                        "setup_fingerprint": order.setup_fingerprint,
                        "entry_mode": order.entry_mode,
                        "pending_entry_price": order.pending_entry_price,
                        "trigger_price": order.trigger_price,
                        "reason_code": ReasonCode.ORDER_EXPIRED.value,
                        "close_time": ts_iso,
                        "metadata": dict(order.metadata),
                    }
                )
                continue
            if bar_index <= int(order.placed_bar_index):
                survivors.append(order)
                continue
            touched = low <= float(order.pending_entry_price) <= high
            if touched:
                trade = self.open_trade(
                    strategy_name=order.strategy_name,
                    symbol=order.symbol,
                    side=order.side,
                    open_time=ts_iso,
                    entry=float(order.pending_entry_price),
                    sl=float(order.metadata["sl"]),
                    tp=float(order.metadata["tp"]),
                    volume=float(order.metadata["volume"]),
                    setup=order.setup,
                    metadata={
                        **dict(order.metadata),
                        "pending_order_id": order.order_id,
                        "pending_entry_price": float(order.pending_entry_price),
                        "entry_mode": order.entry_mode,
                        "setup_fingerprint": order.setup_fingerprint,
                    },
                    tp1=float(order.metadata.get("tp1", order.metadata["tp"])),
                )
                trade.metadata["filled_from_pending_order"] = order.order_id
                events.append(
                    {
                        "event_type": "pending_filled",
                        "order_id": order.order_id,
                        "strategy_name": order.strategy_name,
                        "symbol": order.symbol,
                        "side": order.side,
                        "setup": order.setup,
                        "setup_fingerprint": order.setup_fingerprint,
                        "entry_mode": order.entry_mode,
                        "pending_entry_price": order.pending_entry_price,
                        "trigger_price": order.trigger_price,
                        "trade": trade,
                        "close_time": ts_iso,
                    }
                )
            else:
                survivors.append(order)
        self.pending_orders = survivors
        return events

    def apply_management_actions(
        self,
        trade: SimTrade,
        actions: list[dict[str, Any]],
        current_price: float,
        ts_iso: str,
        symbol_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply risk-manager actions to an open trade."""
        if trade.closed:
            return []
        events: list[dict[str, Any]] = []
        for action in actions:
            action_name = str(action.get("action") or "")
            reason_code = str(action.get("reason_code") or "management_action")
            if action_name == ManagementAction.PARTIAL_CLOSE.value:
                partial_volume = float(action.get("volume") or 0.0)
                partial_pnl = trade.register_partial_close(
                    close_price=float(trade.tp1 if trade.tp1 else current_price),
                    partial_volume=partial_volume,
                    contract_size=self.contract_size,
                    ts_iso=ts_iso,
                    reason_code=reason_code,
                )
                events.append(
                    {
                        "event_type": ManagementAction.PARTIAL_CLOSE.value,
                        "reason_code": reason_code,
                        "volume": partial_volume,
                        "pnl": partial_pnl,
                        "partial_realized_pnl": float(trade.partial_realized_pnl),
                        "trade": trade,
                    }
                )
            elif action_name == ManagementAction.MOVE_STOP.value:
                new_sl = float(action.get("sl") or trade.sl)
                if self.update_trade_stop(trade, new_sl, ts_iso, reason_code):
                    events.append(
                        {
                            "event_type": ManagementAction.MOVE_STOP.value,
                            "reason_code": reason_code,
                            "sl": new_sl,
                            "trade": trade,
                        }
                    )
            elif action_name == ManagementAction.CLOSE_FULL.value:
                exit_price = float(action.get("exit_price") or current_price)
                close_reason = str(action.get("human_reason") or reason_code)
                events.append(
                    {
                        "event_type": ManagementAction.CLOSE_FULL.value,
                        "reason_code": reason_code,
                        "close_reason": close_reason,
                        "payload": self.close_trade_now(
                            trade=trade,
                            exit_price=exit_price,
                            exit_reason=reason_code,
                            close_time=ts_iso,
                            reason_code=reason_code,
                            extra_metadata=dict(action.get("metadata") or {}),
                        ),
                    }
                )
                break
        return events

    def on_bar(self, bar: dict[str, Any], ts_iso: str) -> list[dict[str, Any]]:
        """Update open trades for one completed bar and return closed trade payloads."""
        low = float(bar["low"])
        high = float(bar["high"])
        closed: list[dict[str, Any]] = []
        survivors: list[SimTrade] = []

        for trade in self.open_trades:
            if trade.closed:
                continue
            if str(trade.open_time) == ts_iso:
                survivors.append(trade)
                continue
            direction = _direction(trade.side)
            favorable = (high - trade.entry) if direction > 0 else (trade.entry - low)
            adverse = (trade.entry - low) if direction > 0 else (high - trade.entry)
            trade.max_favorable = max(trade.max_favorable, favorable)
            trade.max_adverse = max(trade.max_adverse, adverse)

            exit_reason = self._resolve_hit(trade, low, high)
            if exit_reason is None:
                survivors.append(trade)
                continue

            exit_price = self._bar_exit_price(trade, exit_reason)
            payload = trade.close_full(
                exit_price=exit_price,
                ts_iso=ts_iso,
                exit_reason="TP_HIT" if exit_reason == "tp" else "SL_HIT",
                reason_code=ReasonCode.TAKE_PROFIT_EXIT.value if exit_reason == "tp" else ReasonCode.STOP_LOSS_EXIT.value,
                contract_size=self.contract_size,
                commission=self._commission_for_trade(trade),
                swap=float(trade.metadata.get("swap", 0.0) or 0.0),
            )
            closed.append(payload)

        self.open_trades = survivors
        return closed

    def floating_pnl(self, close_price: float) -> float:
        """Return current floating PnL from open positions at a mark price."""
        mark = float(close_price)
        total = 0.0
        for trade in self.open_trades:
            if trade.closed:
                continue
            direction = _direction(trade.side)
            total += (mark - trade.entry) * direction * trade.volume * self.contract_size
        return total
