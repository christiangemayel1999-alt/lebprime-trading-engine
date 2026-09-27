from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol


class LebPrimeRiskService(Protocol):
    """Risk capabilities consumed by LebPrime after strategy intent exists."""

    def evaluate_risk_gate(
        self,
        state: dict[str, Any],
        account_info: Any,
        now_utc: datetime,
        session_name: str,
        live_requested: bool,
        force_reduced_risk: bool = False,
    ) -> dict[str, Any]: ...

    def calculate_position_size(
        self,
        account_info: Any,
        trade_plan: dict[str, Any],
        symbol_spec: dict[str, Any],
        live_requested: bool,
        reduced_risk: bool,
        entry_mode: str,
        risk_adjustment: float = 1.0,
    ) -> dict[str, Any]: ...

    def fit_volume_to_margin(
        self,
        requested_volume: float,
        account_info: Any,
        symbol_spec: dict[str, Any],
        margin_for_volume: Callable[[float], float],
        diagnostic_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
