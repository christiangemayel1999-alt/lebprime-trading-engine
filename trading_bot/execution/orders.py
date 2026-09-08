from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class OrderRequest:
    strategy_name: str
    symbol: str
    side: str
    order_type: str
    entry_price: float
    volume: float
    stop_loss: float
    take_profit: float
    expiry_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

