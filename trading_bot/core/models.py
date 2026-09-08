from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from trading_bot.core.enums import OrderStatus


@dataclass(slots=True)
class MarketContext:
    timestamp: str
    session: dict[str, Any]
    bias: dict[str, Any]
    regime: dict[str, Any]
    spread_points: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StrategyCandidate:
    strategy_name: str
    setup_family: str
    side: str
    symbol: str
    timestamp: str
    setup_valid: bool
    setup_score: float
    trend_score: float
    regime_name: str
    regime_confidence: float
    session_name: str
    session_quality_score: float
    value_price: float | None = None
    entry_price: float | None = None
    structure_level: float | None = None
    atr_at_setup: float | None = None
    reason_code: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], *, strategy_name: str | None = None, symbol: str | None = None) -> "StrategyCandidate":
        metadata = dict(payload)
        return cls(
            strategy_name=str(strategy_name or payload.get("strategy_name") or payload.get("signal_name") or ""),
            setup_family=str(payload.get("setup_family") or ""),
            side=str(payload.get("side") or ""),
            symbol=str(symbol or payload.get("symbol") or "XAUUSD"),
            timestamp=str(payload.get("anchor_time") or payload.get("timestamp") or datetime.utcnow().isoformat()),
            setup_valid=bool(payload.get("setup_valid")),
            setup_score=float(payload.get("setup_score", 0.0) or 0.0),
            trend_score=float(payload.get("trend_score", 0.0) or 0.0),
            regime_name=str(payload.get("regime_name") or ""),
            regime_confidence=float(payload.get("regime_confidence", 0.0) or 0.0),
            session_name=str(payload.get("session_name") or ""),
            session_quality_score=float(payload.get("session_quality_score", 0.0) or 0.0),
            value_price=_optional_float(payload.get("value_price")),
            entry_price=_optional_float(payload.get("entry_price") or payload.get("setup_price")),
            structure_level=_optional_float(payload.get("structure_level")),
            atr_at_setup=_optional_float(payload.get("atr_at_setup")),
            reason_code=str(payload.get("reason_code") or ""),
            reason=str(payload.get("reason") or ""),
            metadata=metadata,
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        payload.update(
            {
                "strategy_name": self.strategy_name,
                "setup_family": self.setup_family,
                "side": self.side,
                "symbol": self.symbol,
                "setup_valid": self.setup_valid,
                "setup_score": self.setup_score,
                "trend_score": self.trend_score,
                "regime_name": self.regime_name,
                "regime_confidence": self.regime_confidence,
                "session_name": self.session_name,
                "session_quality_score": self.session_quality_score,
                "reason_code": self.reason_code,
                "reason": self.reason,
            }
        )
        if self.value_price is not None:
            payload["value_price"] = self.value_price
        if self.entry_price is not None:
            payload.setdefault("entry_price", self.entry_price)
            payload.setdefault("setup_price", self.entry_price)
        if self.structure_level is not None:
            payload["structure_level"] = self.structure_level
        if self.atr_at_setup is not None:
            payload["atr_at_setup"] = self.atr_at_setup
        return payload


@dataclass(slots=True)
class EntryEvaluation:
    valid: bool
    live_ready: bool
    entry_mode: str
    entry_price: float | None
    reason_code: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason_code: str
    volume: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionRequest:
    strategy_name: str
    symbol: str
    side: str
    order_type: str
    entry_price: float
    volume: float
    stop_loss: float
    take_profit: float
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderResult:
    ok: bool
    status: str
    order_id: str | None = None
    position_id: str | None = None
    reason_code: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.setdefault("status", self.status or OrderStatus.REJECTED.value)
        return payload


@dataclass(slots=True)
class PositionState:
    position_id: str
    strategy_name: str
    symbol: str
    side: str
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: str
    lifecycle_state: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CloseResult:
    position_id: str
    closed_at: str
    exit_price: float
    pnl: float
    pnl_r: float
    exit_reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CloseDecision:
    close: bool
    reason_code: str
    position_id: str
    volume: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionEvent:
    event_type: str
    timestamp: str
    symbol: str
    reason_code: str = ""
    order_id: str | None = None
    position_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JournalEvent:
    event_type: str
    timestamp: str
    source: str
    symbol: str
    reason_code: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AccountState:
    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    trade_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mt5(cls, account_info: Any) -> "AccountState":
        return cls(
            balance=float(getattr(account_info, "balance", 0.0) or 0.0),
            equity=float(getattr(account_info, "equity", 0.0) or 0.0),
            margin=float(getattr(account_info, "margin", 0.0) or 0.0),
            margin_free=float(getattr(account_info, "margin_free", 0.0) or 0.0),
            margin_level=float(getattr(account_info, "margin_level", 0.0) or 0.0),
            trade_allowed=bool(getattr(account_info, "trade_allowed", False)),
            metadata=_serialize_mapping(account_info),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _serialize_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if isinstance(value, dict):
        return dict(value)
    return {}
