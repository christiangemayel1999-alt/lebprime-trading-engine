from __future__ import annotations

from typing import Any, Protocol


class MarketDataGateway(Protocol):
    """Broker market-data and symbol capabilities used by LebPrime."""

    def ensure_symbol_selected(self, symbol: str, log_details: bool = False) -> bool: ...

    def fetch_rates(self, timeframe: str, count: int, **kwargs: Any) -> Any: ...

    def get_tick(self) -> Any: ...

    def get_symbol_spec(self, force_refresh: bool = False) -> dict[str, Any]: ...

    def spread_check_stage(self, spread_limit: float) -> dict[str, Any]: ...


class AccountGateway(Protocol):
    """Account and margin capabilities used before broker submission."""

    def get_account_info(self) -> Any: ...

    def get_terminal_info(self) -> Any: ...

    def calculate_margin(self, order_type: Any, volume: float, price: float) -> float: ...


class PendingOrderGateway(Protocol):
    """Existing pending-order pipeline, exposed without reimplementing it."""

    def pending_order_type(self, direction: str) -> Any: ...

    def build_pending_limit_request(self, **kwargs: Any) -> tuple[Any, Any, Any]: ...

    def precheck_order(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def send_order(self, request: dict[str, Any], dry_run: bool = False) -> dict[str, Any]: ...

    def cancel_pending_order(self, ticket: Any) -> tuple[bool, Any]: ...


class BrokerStateGateway(Protocol):
    """Current broker order and position state."""

    def get_pending_orders(self) -> list[Any]: ...

    def get_bot_positions(self) -> list[Any]: ...

    def find_bot_position(self, order_ticket: Any, comment: str) -> Any: ...


class BrokerHistoryGateway(Protocol):
    """Broker-authoritative order and deal history for reconciliation."""

    def get_bot_order_history(self) -> list[Any]: ...

    def get_bot_deal_history(self) -> list[Any]: ...

    def get_position_deals(self, position_ticket: Any, opened_at: Any = None) -> list[Any]: ...


class PositionManagementGateway(Protocol):
    """Position actions currently required by LebPrime lifecycle events."""

    def close_partial(self, ticket: Any, volume: float, comment: str = "") -> tuple[bool, Any]: ...

    def close_position(self, ticket: Any, comment: str = "") -> tuple[bool, Any]: ...


class LebPrimeBrokerGateway(
    MarketDataGateway,
    AccountGateway,
    PendingOrderGateway,
    BrokerStateGateway,
    BrokerHistoryGateway,
    PositionManagementGateway,
    Protocol,
):
    """Combined structural contract for the current LebPrime live service."""

    mt5: Any
    symbol: str

    def ensure_initialized(self) -> bool: ...

    def ensure_connection(self) -> bool: ...

    def is_connected(self) -> bool: ...

    def shutdown(self) -> None: ...
