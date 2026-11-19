"""Pine parity fixes and the TradingView export comparison tool."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import Candle, ZoneRecord
from services.indicator_lab.parameter_registry import default_parameters
from services.indicator_lab.parity import compare_tradingview_export, python_event_grid


def _random_walk(bars: int = 3000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 2000 + rng.normal(0, 1.2, bars).cumsum()
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.random(bars) * 0.8
    low = np.minimum(open_, close) - rng.random(bars) * 0.8
    times = pd.date_range("2025-01-02", periods=bars, freq="15min", tz="UTC")
    return pd.DataFrame({"timestamp_utc": times.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": open_.round(2), "high": high.round(2), "low": low.round(2), "close": close.round(2)})


def _tradingview_csv(tmp_path, frame: pd.DataFrame, grid: pd.DataFrame):
    export = pd.DataFrame({"time": (pd.to_datetime(frame["timestamp_utc"]).astype("int64") // 10**9), "open": frame["open"], "high": frame["high"], "low": frame["low"], "close": frame["close"]})
    export = pd.concat([export, grid], axis=1)
    path = tmp_path / "tv_export.csv"
    export.to_csv(path, index=False)
    return path, export


def test_atr_is_wilder_rma_like_pine():
    engine = LebPrimeEngine(symbol="XAUUSD")
    ranges = [2.0, 4.0, 6.0, 8.0]
    for i, rng in enumerate(ranges):
        engine._bars.append(Candle(i, f"2026-01-01T00:0{i}:00+00:00", 100.0, 100.0 + rng, 100.0, 100.0))
    # Seed = SMA of the first 3 true ranges = 4.0; then (8 + 2 * 4) / 3.
    engine_three = LebPrimeEngine(symbol="XAUUSD")
    engine_three._bars = engine._bars[:3]
    assert engine_three._atr(3) == pytest.approx(4.0)
    engine_three._bars = engine._bars
    assert engine_three._atr(3) == pytest.approx((8.0 + 2 * 4.0) / 3.0)
    assert engine_three._atr(3) == pytest.approx((8.0 + 2 * 4.0) / 3.0)  # repeat call on the same bar is stable


def test_after_limit_created_close_mode_cancels_on_close_not_open():
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), "execution_model": "Pine indicator"})
    zone = ZoneRecord(
        engine_zone_id="z", side="DEMAND", source_bar_index=-20, source_time="t", zone_top=100.0, zone_bottom=99.0,
        created_bar_index=0, created_time="t", limit_created_bar_index=0, limit_created_time="t",
        entry1=100.0, entry2=99.0, entry_price=100.0, sl_price=98.0, tp1_price=102.0, tp2_price=104.0, tp_price=104.0,
    )
    engine._zones = [zone]
    # Opens below the zone but closes back inside: Pine keeps the limit.
    engine._bars = [Candle(0, "t0", 99.5, 100.5, 99.4, 99.6), Candle(1, "t1", 98.8, 99.8, 98.7, 99.5)]
    engine._manage_zones(engine._bars[-1])
    assert zone.terminal_state is None
    engine._bars.append(Candle(2, "t2", 99.5, 99.6, 98.5, 98.8))
    engine._manage_zones(engine._bars[-1])
    assert zone.terminal_state == "CANCELED"


def test_history_window_only_limits_detection_when_enabled():
    frame = _random_walk(1500)
    base = {**default_parameters("LEBPRIME"), "execution_model": "Pine indicator"}
    full = LebPrimeEngine(base).run(frame)
    windowed = LebPrimeEngine({**base, "apply_history_window": True, "history_bars": 500}).run(frame)
    assert len(windowed["zones"]) < len(full["zones"])
    assert all(zone.created_bar_index > len(frame) - 1 - 500 for zone in windowed["zones"])


def test_parity_tool_reports_full_match_and_catches_differences(tmp_path):
    frame = _random_walk()
    grid = python_event_grid(frame, symbol="XAUUSD", timeframe="M15", history_bars=5000)
    assert grid.notna().to_numpy().sum() > 50  # the walk produces real signals
    path, export = _tradingview_csv(tmp_path, frame, grid)

    report = compare_tradingview_export(path, symbol="XAUUSD", timeframe="M15", history_bars=5000)
    assert report["parity"] is True
    assert report["mismatch_count"] == 0

    row = int(export["LP B CREATED"].first_valid_index())
    export.loc[row, "LP B SL"] = export.loc[row, "LP B SL"] - 1.0
    export.to_csv(path, index=False)
    report = compare_tradingview_export(path, symbol="XAUUSD", timeframe="M15", history_bars=5000)
    assert report["parity"] is False
    assert report["mismatch_count"] == 1
    assert report["examples"][0]["slot"] == "LP B SL"


def test_parity_tool_rejects_a_normal_chart_export(tmp_path):
    path = tmp_path / "plain.csv"
    pd.DataFrame({"time": [1, 2], "open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="parity"):
        compare_tradingview_export(path, symbol="XAUUSD")


def test_parity_table_check_matches_engine_rows():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("check_parity_table", Path(__file__).resolve().parents[1] / "scripts" / "check_parity_table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    frame = _random_walk()
    params = {**default_parameters("LEBPRIME"), "execution_model": "Pine indicator"}
    zones = [z for z in LebPrimeEngine(params).run(frame)["zones"] if z.limit_created_bar_index is not None][-10:]
    assert zones
    rows = pd.DataFrame(
        [
            {
                "created_utc": pd.Timestamp(z.limit_created_time).strftime("%Y-%m-%d %H:%M"),
                "side": "BUY" if z.side == "DEMAND" else "SELL",
                "entry1": z.entry_price, "sl": z.sl_price, "tp1": z.tp1_price, "tp2": z.tp2_price,
                "status": module.python_status(z) + " 2025-01-02 10:00",
            }
            for z in zones
        ]
    )
    report = module.compare(rows, frame, symbol="XAUUSD", timeframe="M15", tolerance=1e-9)
    assert (report["match"] == "OK").all()

    rows.loc[0, "sl"] = rows.loc[0, "sl"] + 5
    report = module.compare(rows, frame, symbol="XAUUSD", timeframe="M15", tolerance=0.01)
    assert report.loc[0, "match"] == "LEVELS_DIFFER"
