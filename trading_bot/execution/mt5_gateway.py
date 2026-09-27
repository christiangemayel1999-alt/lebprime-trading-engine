from __future__ import annotations

from typing import Any

from mt5_connector import MT5Connector


class MT5BrokerGateway:
    """Capability adapter that delegates unchanged behavior to MT5Connector."""

    def __init__(self, connector: MT5Connector) -> None:
        self.connector = connector

    @property
    def mt5(self) -> Any:
        return self.connector.mt5

    @property
    def symbol(self) -> str:
        return str(self.connector.symbol)

    def ensure_initialized(self) -> bool:
        return self.connector.ensure_initialized()

    def ensure_connection(self) -> bool:
        return self.connector.ensure_connection()

    def is_connected(self) -> bool:
        return self.connector.is_connected()

    def shutdown(self) -> None:
        self.connector.shutdown()

    def ensure_symbol_selected(self, symbol: str, log_details: bool = False) -> bool:
        return self.connector.ensure_symbol_selected(symbol, log_details=log_details)

    def fetch_rates(self, timeframe: str, count: int, **kwargs: Any) -> Any:
        return self.connector.fetch_rates(timeframe, count, **kwargs)

    def get_tick(self) -> Any:
        return self.connector.get_tick()

    def get_symbol_spec(self, force_refresh: bool = False) -> dict[str, Any]:
        return self.connector.get_symbol_spec(force_refresh=force_refresh)

    def spread_check_stage(self, spread_limit: float) -> dict[str, Any]:
        return self.connector.spread_check_stage(spread_limit)

    def get_account_info(self) -> Any:
        return self.connector.get_account_info()

    def get_terminal_info(self) -> Any:
        return self.connector.get_terminal_info()

    def calculate_margin(self, order_type: Any, volume: float, price: float) -> float:
        return self.connector.calculate_margin(order_type, volume, price)

    def pending_order_type(self, direction: str) -> Any:
        return self.connector.pending_order_type(direction)

    def build_pending_limit_request(self, **kwargs: Any) -> tuple[Any, Any, Any]:
        return self.connector.build_pending_limit_request(**kwargs)

    def precheck_order(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.connector.precheck_order(request)

    def send_order(self, request: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        return self.connector.send_order(request, dry_run=dry_run)

    def cancel_pending_order(self, ticket: Any) -> tuple[bool, Any]:
        return self.connector.cancel_pending_order(ticket)

    def get_pending_orders(self) -> list[Any]:
        return self.connector.get_pending_orders()

    def get_bot_positions(self) -> list[Any]:
        return self.connector.get_bot_positions()

    def find_bot_position(self, order_ticket: Any, comment: str) -> Any:
        return self.connector.find_bot_position(order_ticket, comment)

    def get_bot_order_history(self) -> list[Any]:
        return self.connector.get_bot_order_history()

    def get_bot_deal_history(self) -> list[Any]:
        return self.connector.get_bot_deal_history()

    def get_position_deals(self, position_ticket: Any, opened_at: Any = None) -> list[Any]:
        return self.connector.get_position_deals(position_ticket, opened_at)

    def close_partial(self, ticket: Any, volume: float, comment: str = "") -> tuple[bool, Any]:
        return self.connector.close_partial(ticket, volume, comment=comment)

    def close_position(self, ticket: Any, comment: str = "") -> tuple[bool, Any]:
        return self.connector.close_position(ticket, comment=comment)
