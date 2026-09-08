from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trading_bot.execution.canonical import CanonicalExecutionPlan


def candles(
    *,
    start: datetime | str = datetime(2026, 4, 1, tzinfo=timezone.utc),
    periods: int = 64,
    base: float = 100.0,
    step: float = 0.05,
) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq="min", tz="UTC")
    rows: list[dict[str, Any]] = []
    for index, ts in enumerate(times):
        open_price = base + index * step
        close_price = open_price + 0.02
        rows.append(
            {
                "time": ts,
                "open": open_price,
                "high": close_price + 0.08,
                "low": open_price - 0.08,
                "close": close_price,
                "tick_volume": 100 + index,
                "spread": 0.0,
                "real_volume": 150 + index,
            }
        )
    return pd.DataFrame(rows)


def flat_no_signal_candles(periods: int = 80) -> pd.DataFrame:
    frame = candles(periods=periods, base=100.0, step=0.0)
    frame["open"] = 100.0
    frame["high"] = 100.05
    frame["low"] = 99.95
    frame["close"] = 100.0
    return frame


def symbol_spec(contract_size: float = 1.0, volume_min: float = 0.1) -> dict[str, float]:
    return {
        "point": 0.1,
        "pip_size": 0.1,
        "digits": 2,
        "contract_size": contract_size,
        "volume_min": volume_min,
        "volume_max": 10.0,
        "volume_step": volume_min,
        "stops_level": 0.0,
        "freeze_level": 0.0,
        "filling_mode_raw": 0,
        "order_mode": 0,
        "filling_modes": [0],
        "pip_value_per_lot": 1.0,
    }


def candidate(ts: pd.Timestamp, fingerprint: str = "fp-1", *, side: str = "LONG") -> dict[str, Any]:
    return {
        "setup_valid": True,
        "setup_family": "COMPRESSION_RELEASE",
        "setup_score": 70.0,
        "trend_score": 70.0,
        "session_quality_score": 90.0,
        "regime_confidence": 90.0,
        "side": side,
        "atr_at_setup": 1.0,
        "structure_level": 99.0 if side == "LONG" else 101.0,
        "value_price": 100.0,
        "entry_price": 100.0,
        "setup_fingerprint": fingerprint,
        "trigger_type": "COMPRESSION_RELEASE",
        "strategy_control": {},
        "regime_name": "TREND_CONTINUATION",
        "session_name": "LONDON",
        "anchor_time": ts.isoformat(),
    }


def canonical_plan(**overrides: Any) -> CanonicalExecutionPlan:
    values: dict[str, Any] = {
        "strategy_name": "XAU_BOT_COMPRESS",
        "symbol": "XAUUSD",
        "side": "LONG",
        "order_type": "market",
        "entry_price": 100.5,
        "volume": 0.5,
        "stop_loss": 100.0,
        "take_profit": 101.3,
        "setup_family": "COMPRESSION_RELEASE",
        "setup_fingerprint": "fp-parity",
        "entry_mode": "confirmed",
        "execution_model": "next_bar_open",
        "signal_time": "2026-04-01T00:55:00+00:00",
        "execution_time": "2026-04-01T00:56:00+00:00",
        "signal_bar_time": "2026-04-01T00:55:00+00:00",
        "signal_bar_close": 100.4,
        "execution_bar_time": "2026-04-01T00:56:00+00:00",
        "executable_entry": 100.5,
        "spread_used": 0.0,
        "slippage_used": 0.0,
        "fill_side": "ask",
        "data_available_through_time": "2026-04-01T00:55:00+00:00",
        "tp1": 100.9,
        "trigger_price": 100.4,
        "metadata": {
            "execution_reason_code": "entry_valid",
            "risk_metadata": {"risk_amount": 50.0, "risk_percent": 0.5},
        },
    }
    values.update(overrides)
    return CanonicalExecutionPlan(**values)
