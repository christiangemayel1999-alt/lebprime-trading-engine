from __future__ import annotations

from dataclasses import dataclass

from trading_bot.core.enums import RuntimeMode


@dataclass(slots=True)
class RuntimeContext:
    mode: RuntimeMode
    symbol: str
    dry_run: bool
    allow_live_execution: bool

