from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from services.backtest_replay import ReplayClock, ReplayMarketData, SimulatedAccount
from services.historical_feed import HistoricalMT5Feed


pytestmark = pytest.mark.fast


def _bars(start: datetime, count: int, step: timedelta) -> pd.DataFrame:
    rows = []
    for idx in range(count):
        ts = start + idx * step
        rows.append(
            {
                "time": ts,
                "open": 100.0 + idx,
                "high": 101.0 + idx,
                "low": 99.0 + idx,
                "close": 100.5 + idx,
                "tick_volume": 10 + idx,
                "spread": 2,
            }
        )
    return pd.DataFrame(rows)


def test_replay_clock_advances_one_bar_at_a_time() -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc), 3, timedelta(minutes=1))
    clock = ReplayClock(HistoricalMT5Feed(bars), total_bars=len(bars))

    assert clock.bar_index == -1
    assert clock.peek_next_bar()["time"] == bars.iloc[0]["time"]
    assert clock.bar_index == -1

    first = clock.next_bar()
    second = clock.next_bar()

    assert first["time"] == bars.iloc[0]["time"]
    assert second["time"] == bars.iloc[1]["time"]
    assert clock.get_current_bar()["time"] == bars.iloc[1]["time"]
    assert clock.bar_index == 1
    assert clock.progress_total == 3


def test_replay_market_data_exposes_only_closed_higher_timeframe_bars() -> None:
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    trigger_df = _bars(start, 80, timedelta(minutes=1))
    setup_df = _bars(start, 8, timedelta(minutes=15))
    trend_df = _bars(start, 3, timedelta(hours=1))
    market_data = ReplayMarketData(
        trend_df=trend_df,
        setup_df=setup_df,
        trigger_df=trigger_df,
        setup_tf="15min",
        trend_tf="60min",
    )

    current_ts = start + timedelta(hours=1)
    window = market_data.window_at(current_ts)

    assert window.trigger["time"].max() <= current_ts
    assert (window.setup["time"] + pd.Timedelta("15min")).max() <= current_ts
    assert (window.trend["time"] + pd.Timedelta("60min")).max() <= current_ts
    assert start + timedelta(minutes=45) in set(window.setup["time"])
    assert start + timedelta(hours=1) not in set(window.trend["time"])


def test_simulated_account_refresh_uses_executor_floating_pnl() -> None:
    trade = SimpleNamespace(entry=100.0, volume=0.5, closed=False)
    executor = SimpleNamespace(open_trades=[trade], floating_pnl=lambda mark_price: 12.5)
    account = SimulatedAccount(balance=10000.0, equity=10000.0, leverage=100.0)

    account.refresh(
        executor=executor,
        mark_price=105.0,
        now_utc=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
        contract_size=100.0,
    )

    assert account.margin == 50.0
    assert account.equity == 10012.5
    assert account.margin_free == 9962.5
    assert account.peak_equity == 10012.5
