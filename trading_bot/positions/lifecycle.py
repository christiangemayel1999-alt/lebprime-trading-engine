from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.enums import PositionLifecycleState


@dataclass(slots=True)
class LifecycleEvent:
    state: PositionLifecycleState
    reason_code: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)

