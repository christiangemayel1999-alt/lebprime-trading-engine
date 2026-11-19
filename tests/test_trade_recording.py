"""Phase 3: market values recorded at each LebPrime lifecycle moment."""

from __future__ import annotations

import numpy as np
import pandas as pd

from services.indicator_lab.features import FEATURE_KEYS, MOMENTS, FeatureTracker, feature_columns
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import Candle


def _walk(bars: int = 3000, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 2000 + rng.normal(0, 1.2, bars).cumsum()
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.random(bars) * 0.8
    low = np.minimum(open_, close) - rng.random(bars) * 0.8
    times = pd.date_range("2025-01-02", periods=bars, freq="15min", tz="UTC")
    return pd.DataFrame({"timestamp_utc": times.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": open_, "high": high, "low": low, "close": close, "tick_volume": 100 + rng.integers(0, 50, bars), "spread": 20})


def test_rsi_and_dmi_follow_their_definitions():
    tracker = FeatureTracker()
    price = 100.0
    for i in range(40):  # steady rise: RSI 100, +DI dominates
        tracker.update(Candle(i, f"2025-01-02T{i // 4:02d}:{(i % 4) * 15:02d}:00+00:00", price, price + 1.2, price - 0.1, price + 1.0, 100))
        price += 1.0
    snap = tracker.snapshot()
    assert snap["rsi14"] == 100.0
    assert snap["plus_di14"] > snap["minus_di14"]
    assert snap["adx14"] > 50
    assert snap["macd_hist"] is not None


def test_snapshots_use_only_past_candles():
    frame = _walk(600)
    full, cut = FeatureTracker(), FeatureTracker()
    rows = [Candle.from_row(i, row) for i, row in enumerate(frame.to_dict("records"))]
    for row in rows:
        full.update(row)
        if row.index == 400:
            at_400 = full.snapshot()
    for row in rows[:401]:
        cut.update(row)
    assert cut.snapshot() == at_400


def test_every_closed_trade_records_values_at_each_moment():
    result = LebPrimeEngine(symbol="XAUUSD", timeframe="M15").run(_walk())
    closed = [z for z in result["zones"] if z.terminal_state in {"SL_HIT", "TP1_FINAL", "TP2_HIT"}]
    assert closed
    for zone in closed:
        for moment in ("candidate", "limit", "entry1", "exit"):
            assert zone.metadata[f"{moment}_time"] is not None
            assert f"{moment}_rsi14" in zone.metadata and f"{moment}_adx14" in zone.metadata
        assert zone.metadata["exit_reason"]
        assert zone.metadata["entry1_fill_price"] is not None
        assert zone.metadata["candidate_time"] <= zone.metadata["limit_time"] <= zone.metadata["entry1_time"] <= zone.metadata["exit_time"]


def test_feature_columns_cover_all_moments():
    columns = feature_columns()
    assert len(columns) == len(MOMENTS) * (len(FEATURE_KEYS) + 2)
    assert "entry1_rsi14" in columns and "exit_macd_hist" in columns and "limit_plus_di14" in columns
