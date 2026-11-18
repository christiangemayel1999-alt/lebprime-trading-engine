"""Live broker execution model for the LebPrime engine, symbol map and dataset closing rules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from services.config_manager import ConfigManager
from services.indicator_lab.dataset_service import drop_unclosed_bars
from services.indicator_lab.lebprime_engine import LebPrimeEngine, is_forex_symbol
from services.indicator_lab.models import Candle, ZoneRecord
from services.indicator_lab.parameter_registry import default_parameters
from services.lebprime_live.symbols import DEFAULT_SYMBOLS, SYMBOLS, resolve_symbol

ROOT = Path(__file__).resolve().parents[1]
PINE = "Pine indicator"


def _engine(model: str | None = None, symbol: str = "XAUUSD") -> LebPrimeEngine:
    params = default_parameters("LEBPRIME")
    if model:
        params["execution_model"] = model
    return LebPrimeEngine(params, symbol=symbol)


def _demand_zone() -> ZoneRecord:
    # Entry 1 = 100, Entry 2 = 99, SL = 98, TP1 = 102, TP2 = 104 (risk 2 from Entry 1).
    return ZoneRecord(
        engine_zone_id="demand-live",
        side="DEMAND",
        source_bar_index=-20,
        source_time="2026-08-01T00:00:00+00:00",
        zone_top=100.0,
        zone_bottom=99.0,
        created_bar_index=0,
        created_time="2026-08-01T00:00:00+00:00",
        limit_created_bar_index=0,
        limit_created_time="2026-08-01T00:00:00+00:00",
        entry1=100.0,
        entry2=99.0,
        entry_price=100.0,
        sl_price=98.0,
        tp1_price=102.0,
        tp2_price=104.0,
        tp_price=104.0,
    )


def _bar(index: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(index, f"2026-08-01T00:{index:02d}:00+00:00", o, h, low, c)


def _run(engine: LebPrimeEngine, zone: ZoneRecord, bars: list[Candle]) -> list[str]:
    engine._zones = [zone]
    for bar in bars:
        engine._bars.append(bar)
        engine._manage_zones(bar)
    return [event.event_type for event in engine.events()]


def test_live_model_is_the_default():
    assert default_parameters("LEBPRIME")["execution_model"] == "Live broker"


def test_limit_fills_on_any_touch_like_a_broker_order():
    bars = [_bar(0, 100.5, 101.0, 99.5, 100.6), _bar(1, 100.6, 101.0, 99.9, 100.4)]
    live_zone, pine_zone = _demand_zone(), _demand_zone()
    assert _run(_engine(), live_zone, bars) == ["ENTRY1_ACTIVATED"]
    # Pine only activates when the previous candle was fully outside the zone.
    assert _run(_engine(PINE), pine_zone, bars) == []


def test_stop_is_checked_on_the_entry_candle():
    zone = _demand_zone()
    events = _run(_engine(), zone, [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 97.5, 98.2)])
    assert events == ["ENTRY1_ACTIVATED", "ENTRY2_ACTIVATED", "SL_HIT"]
    assert zone.terminal_state == "SL_HIT"
    assert zone.result_r == pytest.approx(-1.0)


def test_each_entry_carries_half_the_signal_risk():
    engine = _engine()
    zone = _demand_zone()
    legs = engine._live_legs(zone)
    legs["1"].update({"filled": True, "fill_price": 100.0, "open_fraction": 1.0})
    engine._close_live_legs(zone, 98.0, True)
    assert engine._live_result_r(zone) == pytest.approx(-0.5)


def test_gap_through_the_stop_exits_at_the_open():
    zone = _demand_zone()
    bars = [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 99.8, 100.5), _bar(2, 97.0, 97.5, 96.5, 97.2)]
    _run(_engine(), zone, bars)
    # Entry 2 fills at the 97.0 open (better than 99), both legs stop at 97.0.
    assert zone.terminal_state == "SL_HIT"
    assert zone.exit_price == 97.0
    assert zone.result_r == pytest.approx(0.5 * (-3.0 / 2.0) + 0.5 * 0.0)


def test_stop_wins_when_target_and_stop_share_a_candle():
    zone = _demand_zone()
    bars = [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 99.8, 100.5), _bar(2, 100.5, 102.5, 97.9, 101.0)]
    assert _run(_engine(), zone, bars)[-1] == "SL_HIT"
    assert zone.result_r == pytest.approx(-1.0)

    pine_zone = _demand_zone()
    assert _run(_engine(PINE), pine_zone, bars)[-1] == "TP1_HIT"


def test_stop_triggers_on_a_wick_without_a_body_close():
    zone = _demand_zone()
    bars = [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 99.8, 100.5), _bar(2, 100.5, 100.6, 97.9, 100.4)]
    assert _run(_engine(), zone, bars)[-1] == "SL_HIT"
    pine_zone = _demand_zone()
    assert "SL_HIT" not in _run(_engine(PINE), pine_zone, bars)


def test_tp1_partial_and_return_to_entry_use_candle_closes():
    zone = _demand_zone()
    bars = [
        _bar(0, 102, 102.5, 101, 101.5),
        _bar(1, 101.0, 101.5, 99.8, 100.5),   # Entry 1 fills at 100
        _bar(2, 100.5, 102.2, 100.2, 101.8),  # TP1: half closed at 101.8, Entry 2 canceled
        _bar(3, 101.8, 102.0, 99.9, 100.1),   # back to Entry 1: rest closed at 100.1
    ]
    assert _run(_engine(), zone, bars) == ["ENTRY1_ACTIVATED", "TP1_HIT", "TP1_FINAL"]
    expected = 0.5 * 0.5 * (1.8 / 2.0) + 0.5 * 0.5 * (0.1 / 2.0)
    assert zone.result_r == pytest.approx(expected)
    assert zone.metadata["live_legs"]["2"]["canceled"] is True


def test_tp2_after_tp1_and_stop_after_tp1():
    zone = _demand_zone()
    bars = [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 99.8, 100.5), _bar(2, 100.5, 102.2, 100.2, 102.0), _bar(3, 102.0, 104.5, 101.5, 104.2)]
    _run(_engine(), zone, bars)
    assert zone.terminal_state == "TP2_HIT"
    assert zone.result_r == pytest.approx(0.5 * 0.5 * (2.0 / 2.0) + 0.5 * 0.5 * (4.0 / 2.0))

    zone = _demand_zone()
    bars = [_bar(0, 102, 102.5, 101, 101.5), _bar(1, 101.0, 101.5, 99.8, 100.5), _bar(2, 100.5, 102.2, 100.2, 102.0), _bar(3, 101.0, 101.2, 97.0, 97.5)]
    _run(_engine(), zone, bars)
    assert zone.terminal_state == "TP1_FINAL"
    assert zone.result_r == pytest.approx(0.5 * 0.5 * (2.0 / 2.0) - 0.5 * 0.5 * (2.0 / 2.0))


def test_win_rate_counts_profitable_trades_only():
    engine = _engine()
    for name, state, result in (("a", "TP1_FINAL", -0.1), ("b", "TP2_HIT", 1.5), ("c", "SL_HIT", -1.0), ("d", "TP1_FINAL", 0.4)):
        zone = _demand_zone()
        zone.engine_zone_id, zone.terminal_state, zone.result_r = name, state, result
        engine._zones.append(zone)
    metrics = engine.metrics()
    assert metrics["wins"] == 2
    assert metrics["win_rate"] == pytest.approx(50.0)
    assert metrics["tp_touch_rate"] == pytest.approx(75.0)


def test_forex_buffer_matches_pine_for_eurusd():
    assert is_forex_symbol("EURUSD") and is_forex_symbol("USDJPY.m")
    assert not is_forex_symbol("XAUUSD") and not is_forex_symbol("US30") and not is_forex_symbol("USTEC")
    assert _engine(symbol="EURUSD")._buffer() == 0.0005
    assert _engine(symbol="USDJPY")._buffer() == 0.05
    assert _engine(symbol="XAGUSD")._buffer() == 0.10
    assert _engine(symbol="WTI")._buffer() == 0.15
    assert _engine(symbol="USTEC")._buffer() == 15.0


def test_broker_symbol_map_has_all_connected_symbols():
    broker = {mapping.mt5_symbol: mapping.broker_description for mapping in SYMBOLS.values()}
    assert broker == {
        "EURUSD": "Euro vs US Dollar",
        "XAUUSD": "GOLD vs USD",
        "US30": "US Wall Street 30 Index",
        "USTEC": "US Tech 100 Index",
        "WTI": "Spot WTI Light Crude Oil",
        "XAGUSD": "Silver vs USD",
    }
    assert resolve_symbol("USTEC").logical_symbol == "NAS100"
    assert resolve_symbol("eurusd").sl_buffer == 0.0005
    # New symbols are selectable but not traded unless chosen.
    assert DEFAULT_SYMBOLS == ("XAUUSD", "US30", "NAS100")


def test_config_accepts_new_symbols(tmp_path):
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    manager = ConfigManager(tmp_path)
    updated = manager.apply_updates({"lebprime_v3_4_0": {"symbols": ["XAUUSD", "XAGUSD", "WTI", "EURUSD"], "max_spread_points_by_symbol": {"XAGUSD": 40, "WTI": 10, "EURUSD": 20, "USTEC": 80}}})
    manager._validate(updated)
    with pytest.raises(ValueError):
        manager._validate({**updated, "lebprime_v3_4_0": {**updated["lebprime_v3_4_0"], "symbols": ["BTCUSD"]}})


def test_dataset_drops_the_forming_candle():
    frame = pd.DataFrame({"timestamp_utc": ["2026-11-18T17:17:00+00:00", "2026-11-18T17:18:00+00:00", "2026-11-18T17:19:00+00:00"], "open": [1, 1, 1]})
    now = datetime(2026, 11, 18, 17, 19, 53, tzinfo=timezone.utc)
    kept = drop_unclosed_bars(frame, "M1", now)
    assert list(kept["timestamp_utc"]) == ["2026-11-18T17:17:00+00:00", "2026-11-18T17:18:00+00:00"]
    assert len(drop_unclosed_bars(frame, "M15", now)) == 0
