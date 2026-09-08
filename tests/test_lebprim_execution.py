from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import mt5_connector
from lebprim_strategy import LebprimStrategy
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from services.backtest_runner import BacktestRunner
from services.database import DatabaseService
from services.simulated_executor import SimulatedExecutor


def _base_lebprim_config() -> dict[str, object]:
    return {
        "risk": {
            "risk_percent": 0.5,
            "min_balance": 100.0,
            "live_risk_multiplier": 1.0,
            "reduced_risk_multiplier_after_drawdown": 1.0,
            "reduced_risk_trigger_drawdown_pct": 10.0,
            "live_max_lot": 1.0,
            "max_lot": 1.0,
            "min_lot": 0.01,
            "min_practical_volume": 0.01,
            "max_trades_per_day": 8,
            "max_trades_per_symbol": 4,
            "max_daily_drawdown_pct": 10.0,
            "max_session_drawdown_pct": 10.0,
            "max_consecutive_losses": 5,
            "high_vol_atr_ratio_threshold": 10.0,
            "high_vol_risk_multiplier": 1.0,
        },
        "exit": {
            "sl_buffer_atr": 0.25,
            "sl_buffer_pips": 1.0,
            "sl_min_pips": 0.5,
            "sl_max_pips": 50.0,
            "min_sl_atr_multiplier": 1.0,
            "partial_tp_enabled": True,
            "partial_tp_rr": 1.0,
            "partial_close_fraction": 0.5,
            "breakeven_after_tp1": True,
            "breakeven_buffer_pips": 0.0,
            "trailing_enabled": True,
            "trailing_activation_rr": 1.0,
            "trailing_atr_multiplier": 0.5,
            "min_trailing_stop_pips": 0.1,
            "max_trade_duration_minutes": 12,
            "time_stop_min_progress_rr": 0.0,
            "momentum_failure_exit_enabled": True,
            "momentum_failure_bars": 3,
            "structure_break_exit_enabled": True,
            "trailing_structure_lookback_bars": 3,
            "final_tp_rr": 2.2,
        },
        "cooldowns": {
            "execution_cooldown_minutes": 5,
            "post_loss_cooldown_minutes": 10,
            "consecutive_loss_cooldown_minutes": 30,
        },
    }


def _symbol_spec() -> dict[str, float]:
    return {
        "point": 0.1,
        "digits": 1,
        "pip_size": 0.1,
        "pip_value_per_lot": 1.0,
        "contract_size": 1.0,
        "volume_min": 0.01,
        "volume_max": 10.0,
        "volume_step": 0.01,
        "stops_level": 0.0,
    }


def _trend_df() -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=5, freq="min", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [100, 101, 102, 103, 104],
            "high": [101, 102, 103, 104, 105],
            "low": [99, 100, 101, 102, 103],
            "close": [100, 101, 102, 103, 104],
            "ema_50": [99, 100, 101, 102, 103],
            "ema_200": [98, 99, 100, 101, 102],
            "ema_50_slope_3": [0, 0, 0, 0, 0.5],
            "ema_50_slope_6": [0, 0, 0, 0, 0.7],
            "atr": [1, 1, 1, 1, 1],
        }
    )


def _setup_df() -> pd.DataFrame:
    frame = _trend_df().copy()
    frame["ema_20"] = frame["close"]
    frame["ema_50"] = frame["close"] - 1
    frame["atr_median_20"] = 1.0
    frame["ema_20_slope_2"] = 0.0
    frame["rolling_high_10"] = frame["high"]
    frame["rolling_low_10"] = frame["low"]
    return frame


def _trigger_df() -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=6, freq="min", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [99, 100, 101, 102, 103, 104],
            "high": [100, 101, 102, 103, 104, 106],
            "low": [98, 99, 100, 101, 102, 103],
            "close": [99.5, 100.5, 101.5, 102.5, 103.5, 105.0],
            "ema_20": [99, 100, 101, 102, 103, 104],
            "ema_9": [99, 100, 101, 102, 103, 104],
            "body_ratio": [0.4, 0.4, 0.5, 0.55, 0.6, 0.7],
            "close_location": [0.5, 0.5, 0.55, 0.6, 0.7, 0.8],
            "lower_wick": [0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
            "upper_wick": [0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
            "body": [0.4, 0.4, 0.5, 0.55, 0.6, 0.7],
            "range": [1, 1, 1, 1, 1, 1],
            "volume_ratio": [1.0, 1.0, 1.05, 1.1, 1.2, 1.25],
        }
    )


def test_lebprim_limit_order_fills_only_on_retrace():
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    executor.place_pending_order(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-01-01T00:00:00+00:00",
        setup="LEBPRIM_SCALP",
        setup_fingerprint="fp-1",
        trigger_price=101.0,
        pending_entry_price=100.0,
        volume=1.0,
        sl=98.0,
        tp=104.0,
        tp1=102.0,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    first_bar = {"time": pd.Timestamp("2026-01-01T00:00:00+00:00"), "open": 101.0, "high": 101.5, "low": 99.5, "close": 101.0}
    assert executor.update_pending_orders(first_bar, first_bar["time"].isoformat(), 0) == []

    second_bar = {"time": pd.Timestamp("2026-01-01T00:01:00+00:00"), "open": 101.0, "high": 101.2, "low": 99.8, "close": 100.9}
    events = executor.update_pending_orders(second_bar, second_bar["time"].isoformat(), 1)
    assert len(events) == 1
    assert events[0]["event_type"] == "pending_filled"
    assert len(executor.open_trades) == 1
    assert executor.open_trades[0].open_time == second_bar["time"].isoformat()


def test_lebprim_pending_order_expires():
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    executor.place_pending_order(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="SHORT",
        open_time="2026-01-01T00:00:00+00:00",
        setup="LEBPRIM_SCALP",
        setup_fingerprint="fp-2",
        trigger_price=99.0,
        pending_entry_price=98.0,
        volume=1.0,
        sl=100.0,
        tp=95.0,
        tp1=97.0,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    for idx in range(1, 5):
        bar = {"time": pd.Timestamp("2026-01-01T00:0%d:00+00:00" % idx), "open": 99.0, "high": 99.5, "low": 98.5, "close": 99.0}
        events = executor.update_pending_orders(bar, bar["time"].isoformat(), idx)
        if idx < 4:
            assert events == []
        else:
            assert len(events) == 1
            assert events[0]["event_type"] == "pending_expired"
    assert not executor.open_trades


def test_lebprim_time_stop_closes_trade():
    config = _base_lebprim_config()
    risk_manager = RiskManager(config)
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    trade = executor.open_trade(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-01-01T00:00:00+00:00",
        entry=100.0,
        sl=95.0,
        tp=110.0,
        tp1=105.0,
        volume=1.0,
        setup="LEBPRIM_SCALP",
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    actions = risk_manager.evaluate_management_actions(
        position=trade.position_state(),
        trigger_df=_trigger_df(),
        setup_df=_setup_df(),
        current_price=100.0,
        now_utc=pd.Timestamp("2026-01-01T00:13:00+00:00"),
        symbol_spec=_symbol_spec(),
    )
    assert any(action["reason_code"] == "time_stop_exit" for action in actions)
    applied = executor.apply_management_actions(trade, actions, 100.0, "2026-01-01T00:13:00+00:00", _symbol_spec())
    assert any(event["event_type"] == "close_full" for event in applied)
    assert not executor.open_trades


def test_lebprim_management_actions_apply_in_backtest():
    config = _base_lebprim_config()
    risk_manager = RiskManager(config)
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    trade = executor.open_trade(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-01-01T00:00:00+00:00",
        entry=100.0,
        sl=95.0,
        tp=111.0,
        tp1=105.0,
        volume=1.0,
        setup="LEBPRIM_SCALP",
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    actions = risk_manager.evaluate_management_actions(
        position=trade.position_state(),
        trigger_df=_trigger_df(),
        setup_df=_setup_df(),
        current_price=106.0,
        now_utc=pd.Timestamp("2026-01-01T00:04:00+00:00"),
        symbol_spec=_symbol_spec(),
    )
    applied = executor.apply_management_actions(trade, actions, 106.0, "2026-01-01T00:04:00+00:00", _symbol_spec())
    assert any(event["event_type"] == "partial_close" for event in applied)
    assert any(event["event_type"] == "move_stop" for event in applied)
    assert trade.partial_closed is True
    assert trade.breakeven_moved is True
    assert trade.trailing_active is True
    assert 0.0 < trade.volume < trade.initial_volume


def test_position_exists_not_sticky_after_trade_closed():
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    trade = executor.open_trade(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-01-01T00:00:00+00:00",
        entry=100.0,
        sl=95.0,
        tp=102.0,
        tp1=101.0,
        volume=1.0,
        setup="LEBPRIM_SCALP",
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    assert executor.can_accept_signal("XAUUSD", "LONG", "LEBPRIM_SCALP", "fp-3", "lebprim_limit")[0] is False
    closed = executor.on_bar(
        {"time": pd.Timestamp("2026-01-01T00:01:00+00:00"), "open": 100.0, "high": 102.5, "low": 99.5, "close": 102.2},
        "2026-01-01T00:01:00+00:00",
    )
    assert closed
    assert executor.can_accept_signal("XAUUSD", "LONG", "LEBPRIM_SCALP", "fp-3", "lebprim_limit")[0] is True


def test_pending_order_conflict_handling():
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    executor.place_pending_order(
        strategy_name="XAU_LEBPRIM",
        symbol="XAUUSD",
        side="SHORT",
        open_time="2026-01-01T00:00:00+00:00",
        setup="LEBPRIM_SCALP",
        setup_fingerprint="fp-4",
        trigger_price=100.0,
        pending_entry_price=99.0,
        volume=1.0,
        sl=101.0,
        tp=97.0,
        tp1=98.0,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-01-01T00:00:00+00:00").timestamp()},
    )
    ok, reason = executor.can_accept_signal("XAUUSD", "SHORT", "LEBPRIM_SCALP", "fp-4", "lebprim_limit")
    assert ok is False
    assert reason == "pending_order_exists"
    bar = {"time": pd.Timestamp("2026-01-01T00:06:00+00:00"), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}
    executor.update_pending_orders(bar, bar["time"].isoformat(), 6)
    ok_after, reason_after = executor.can_accept_signal("XAUUSD", "SHORT", "LEBPRIM_SCALP", "fp-4", "lebprim_limit")
    assert ok_after is True
    assert reason_after is None


def test_lebprim_entry_uses_value_price():
    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    config.setdefault("strategy", {})["lebprim_mode"] = "restricted"
    strategy = LebprimStrategy(config)
    trigger_df = _trigger_df()
    candidate = {
        "side": "LONG",
        "atr_at_setup": 1.0,
        "value_price": 102.2,
        "setup_score": 80.0,
        "trend_score": 70.0,
        "session_quality_score": 80.0,
        "regime_confidence": 80.0,
        "family_live_allowed": True,
        "anchor_time": trigger_df.iloc[-1]["time"].isoformat(),
    }
    entry = strategy.evaluate_entry(
        candidate=candidate,
        trigger_df=trigger_df,
        symbol_spec=_symbol_spec(),
        now_utc=trigger_df.iloc[-1]["time"].to_pydatetime(),
        live_profile=True,
    )
    assert entry["entry_price"] == 102.2
    assert entry["pending_entry_price"] == 102.2
    assert entry["trigger_price"] != entry["entry_price"]


def test_backtest_fetch_range_returns_empty_on_mt5_invalid_params():
    class FakeConnector:
        def fetch_rates_range(self, timeframe_label, start_utc, end_utc, min_rows=1):
            raise RuntimeError("copy_rates_range failed: -2 Terminal: Invalid params")

    class FakeLogger:
        def __init__(self):
            self.messages = []

        def warning(self, message):
            self.messages.append(message)

    runner = BacktestRunner.__new__(BacktestRunner)
    runner.logger = FakeLogger()
    frame = runner._fetch_range_or_empty(
        FakeConnector(),
        "1min",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        min_rows=1,
    )

    assert frame.empty
    assert list(frame.columns) == ["time", "open", "high", "low", "close", "tick_volume", "spread"]
    assert "Invalid params" in runner.logger.messages[-1]


def test_mt5_fetch_rates_range_uses_count_fallback_and_filters(monkeypatch):
    class FakeLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, message):
            self.warnings.append(message)

    fallback_calls = []
    start = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(1, 6):
        ts = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
        rows.append(
            {
                "time": int(ts.timestamp()),
                "open": 100.0 + minute,
                "high": 101.0 + minute,
                "low": 99.0 + minute,
                "close": 100.5 + minute,
                "tick_volume": 100,
                "spread": 2,
            }
        )

    def fake_copy_rates_from_pos(*args):
        fallback_calls.append(args)
        return rows

    monkeypatch.setattr(mt5_connector.mt5, "copy_rates_range", lambda *_args: None)
    monkeypatch.setattr(mt5_connector.mt5, "copy_rates_from_pos", fake_copy_rates_from_pos)
    monkeypatch.setattr(mt5_connector.mt5, "last_error", lambda: (-2, "Terminal: Invalid params"))

    logger = FakeLogger()
    connector = MT5Connector({"mt5": {"symbol": "XAUUSD", "timeout": 1}, "logging": {}}, logger)
    frame = connector.fetch_rates_range("1min", start, end, min_rows=1)

    assert len(frame) == 3
    assert frame["time"].min() >= pd.Timestamp(start)
    assert frame["time"].max() <= pd.Timestamp(end)
    assert fallback_calls
    assert any("count-based historical fallback used" in message for message in logger.warnings)


def test_non_lebprim_market_strategies_unchanged(tmp_path, monkeypatch):
    class FakeConnector:
        def __init__(self, config, logger):
            self.config = config
            self.logger = logger

        def initialize(self):
            return True

        def shutdown(self):
            return None

        def get_symbol_spec(self):
            return _symbol_spec()

        def fetch_rates_range(self, timeframe_label, start_utc, end_utc, min_rows=1):
            periods = max(int((end_utc - start_utc).total_seconds() // 60) + 1, min_rows)
            times = pd.date_range(start_utc, periods=periods, freq="min", tz="UTC")
            return pd.DataFrame(
                {
                    "time": times,
                    "open": [100.0 + i * 0.1 for i in range(periods)],
                    "high": [100.2 + i * 0.1 for i in range(periods)],
                    "low": [99.8 + i * 0.1 for i in range(periods)],
                    "close": [100.1 + i * 0.1 for i in range(periods)],
                    "tick_volume": [100] * periods,
                    "spread": [0.2] * periods,
                }
            )

    class FakeStrategy:
        def __init__(self):
            self.emitted = False

        def prepare_trend_dataframe(self, df):
            return df

        def prepare_setup_dataframe(self, df):
            return df

        def prepare_trigger_dataframe(self, df):
            return df

        def analyze_market_context(self, now_utc, trend_df, setup_df, trigger_df, spread_points):
            return {
                "timestamp": now_utc.isoformat(),
                "session": {"session_name": "LONDON", "session_live_allowed": True, "session_quality_score": 90.0},
                "bias": {"direction": "LONG", "confidence": 80.0},
                "regime": {"regime_name": "TREND_CONTINUATION", "regime_confidence": 90.0, "live_allowed": True},
                "spread_points": spread_points,
            }

        def generate_setup_candidates(self, market_context, trend_df, setup_df, trigger_df, symbol_spec):
            if self.emitted:
                return []
            self.emitted = True
            return [
                {
                    "setup_valid": True,
                    "setup_family": "COMPRESSION_RELEASE",
                    "setup_score": 70.0,
                    "trend_score": 70.0,
                    "session_quality_score": 90.0,
                    "regime_confidence": 90.0,
                    "side": "LONG",
                    "atr_at_setup": 2.0,
                    "structure_level": 95.0,
                    "value_price": 100.0,
                    "setup_fingerprint": "fake-non-lebprim",
                    "trigger_type": "COMPRESSION_RELEASE",
                    "strategy_control": {},
                    "regime_name": market_context["regime"]["regime_name"],
                    "session_name": market_context["session"]["session_name"],
                    "hour_utc": 13,
                }
            ]

        def choose_best_setup(self, candidates):
            return candidates[0] if candidates else None

        def evaluate_entry(self, candidate, trigger_df, symbol_spec, now_utc, live_profile):
            return {
                "valid": True,
                "live_ready": True,
                "dry_run_ready": True,
                "entry_mode": "confirmed",
                "trigger_type": "COMPRESSION_RELEASE",
                "entry_price": float(trigger_df.iloc[-1]["close"]),
                "reason_code": "entry_valid",
                "reason": "ok",
                "blocked_reasons": [],
                "trigger_score": 60.0,
                "entry_score": 65.0,
                "trend_score": 70.0,
                "setup_score": 70.0,
                "session_quality_score": 90.0,
                "regime_quality_score": 90.0,
                "entry_threshold": 52.0,
                "min_score_to_trade": 52.0,
                "trigger_candle_atr": 1.0,
                "value_distance_atr": 0.1,
            }

    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    db = DatabaseService(tmp_path / "bot.db")
    monkeypatch.setattr("services.backtest_runner.MT5Connector", FakeConnector)
    monkeypatch.setattr("services.backtest_runner.build_strategy_engine", lambda config, base_dir: FakeStrategy())
    runner = BacktestRunner(tmp_path, config, db)
    bundle = runner.run(
        {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "enabled_strategies": ["XAU_BOT_COMPRESS"],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 0.0},
            "slippage_model": {"type": "fixed_points", "points": 0.0},
            "execution_model": "current_bar_close",
        }
    )
    assert bundle["summary"]["total_trades"] == 1
    assert bundle["summary"]["pending_orders_created"] == 0
    assert bundle["summary"]["pending_orders_filled"] == 0


def test_zombie_trade_does_not_survive_to_forced_end(tmp_path, monkeypatch):
    class FakeConnector:
        def __init__(self, config, logger):
            self.config = config
            self.logger = logger

        def initialize(self):
            return True

        def shutdown(self):
            return None

        def get_symbol_spec(self):
            return _symbol_spec()

        def fetch_rates_range(self, timeframe_label, start_utc, end_utc, min_rows=1):
            periods = max(int((end_utc - start_utc).total_seconds() // 60) + 1, min_rows)
            times = pd.date_range(start_utc, periods=periods, freq="min", tz="UTC")
            return pd.DataFrame(
                {
                    "time": times,
                    "open": [100.0 for _ in range(periods)],
                    "high": [100.2 for _ in range(periods)],
                    "low": [99.8 for _ in range(periods)],
                    "close": [100.0 for _ in range(periods)],
                    "tick_volume": [100] * periods,
                    "spread": [0.0] * periods,
                }
            )

    class FakeStrategy:
        def __init__(self):
            self.emitted = False

        def prepare_trend_dataframe(self, df):
            return df

        def prepare_setup_dataframe(self, df):
            frame = df.copy()
            frame["atr"] = 1.0
            return frame

        def prepare_trigger_dataframe(self, df):
            frame = df.copy()
            frame["ema_20"] = frame["close"]
            frame["range"] = 1.0
            frame["high"] = frame["high"]
            frame["low"] = frame["low"]
            return frame

        def analyze_market_context(self, now_utc, trend_df, setup_df, trigger_df, spread_points):
            return {
                "timestamp": now_utc.isoformat(),
                "session": {"session_name": "LONDON", "session_live_allowed": True, "session_quality_score": 90.0},
                "bias": {"direction": "LONG", "confidence": 80.0},
                "regime": {"regime_name": "TREND_CONTINUATION", "regime_confidence": 90.0, "live_allowed": True},
                "spread_points": spread_points,
            }

        def generate_setup_candidates(self, market_context, trend_df, setup_df, trigger_df, symbol_spec):
            if self.emitted:
                return []
            self.emitted = True
            return [
                {
                    "setup_valid": True,
                    "setup_family": "COMPRESSION_RELEASE",
                    "setup_score": 70.0,
                    "trend_score": 70.0,
                    "session_quality_score": 90.0,
                    "regime_confidence": 90.0,
                    "side": "LONG",
                    "atr_at_setup": 1.0,
                    "structure_level": 95.0,
                    "value_price": 100.0,
                    "setup_fingerprint": "zombie-check",
                    "trigger_type": "COMPRESSION_RELEASE",
                    "strategy_control": {},
                    "regime_name": market_context["regime"]["regime_name"],
                    "session_name": market_context["session"]["session_name"],
                    "hour_utc": 13,
                }
            ]

        def choose_best_setup(self, candidates):
            return candidates[0] if candidates else None

        def evaluate_entry(self, candidate, trigger_df, symbol_spec, now_utc, live_profile):
            return {
                "valid": True,
                "live_ready": True,
                "dry_run_ready": True,
                "entry_mode": "confirmed",
                "trigger_type": "COMPRESSION_RELEASE",
                "entry_price": 100.0,
                "reason_code": "entry_valid",
                "reason": "ok",
                "blocked_reasons": [],
                "trigger_score": 60.0,
                "entry_score": 65.0,
                "trend_score": 70.0,
                "setup_score": 70.0,
                "session_quality_score": 90.0,
                "regime_quality_score": 90.0,
                "entry_threshold": 52.0,
                "min_score_to_trade": 52.0,
                "trigger_candle_atr": 1.0,
                "value_distance_atr": 0.1,
            }

    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    config["exit"]["max_trade_duration_minutes"] = 2
    db = DatabaseService(tmp_path / "bot.db")
    monkeypatch.setattr("services.backtest_runner.MT5Connector", FakeConnector)
    monkeypatch.setattr("services.backtest_runner.build_strategy_engine", lambda config, base_dir: FakeStrategy())
    runner = BacktestRunner(tmp_path, config, db)
    bundle = runner.run(
        {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "enabled_strategies": ["XAU_BOT_COMPRESS"],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 0.0},
            "slippage_model": {"type": "fixed_points", "points": 0.0},
            "execution_model": "current_bar_close",
        }
    )
    assert bundle["trades"]
    assert all(str(trade["exit_reason"]) != "FORCED_END_OF_TEST" for trade in bundle["trades"])
    assert any(str(trade["exit_reason"]) in {"time_stop_exit", "MAX_DURATION_EXIT"} for trade in bundle["trades"])
