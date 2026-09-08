from __future__ import annotations

from enum import Enum


class RuntimeMode(str, Enum):
    LIVE = "LIVE"
    DEMO = "DEMO"
    DRY_RUN = "DRY_RUN"
    BACKTEST = "BACKTEST"
    VALIDATION_TEST = "VALIDATION_TEST"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    CLOSED = "closed"


class PositionLifecycleState(str, Enum):
    CANDIDATE_CREATED = "candidate_created"
    RISK_APPROVED = "risk_approved"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_PENDING = "order_pending"
    ORDER_FILLED = "order_filled"
    POSITION_OPEN = "position_open"
    BREAKEVEN_ACTIVE = "breakeven_active"
    PARTIAL_EXITED = "partial_exited"
    TRAILING_ACTIVE = "trailing_active"
    CLOSE_REQUESTED = "close_requested"
    CLOSED = "closed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"

    # Backward-compatible aliases for Phase 1 callers/tests.
    FILLED = "order_filled"
    OPEN = "position_open"
