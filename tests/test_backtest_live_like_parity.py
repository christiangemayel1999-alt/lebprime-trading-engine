from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from risk_manager import RiskManager
from services.backtest_runner import BacktestRunner
from services.backtest_storage import BacktestStorage
from services.database import DatabaseService
from services.history_loader import HistoryResolution
from services.simulated_executor import SimulatedExecutor
from trading_bot.execution.canonical import CanonicalExecutionPlan, build_execution_request_from_plan

pytestmark = pytest.mark.replay


def _bars(start: datetime, periods: int = 64, base: float = 100.0) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq="min", tz="UTC")
    rows = []
    for index, ts in enumerate(times):
        open_price = base + (index * 0.05)
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


def _base_config() -> dict[str, object]:
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    config["backtest"]["warmup_bars"] = 10
    config["backtest"]["execution_model"] = "next_bar_open"
    config["execution"]["backtest_fill_model"] = "next_bar_open"
    config["risk"]["backtest_account_protection"] = {
        "enabled": False,
        "stop_when_equity_below": 0.0,
        "max_total_drawdown_percent": None,
        "max_daily_drawdown_percent": None,
        "max_consecutive_losses": None,
        "enforce_margin": True,
    }
    config["risk"]["weekend_protection"] = {
        "enabled": False,
        "friday_hard_close_time_utc": "21:00",
        "block_new_entries_after_utc": "20:30",
        "cancel_pending_orders": True,
        "close_open_positions": True,
    }
    config["execution"]["setup_fingerprint_guard"] = {
        "enabled": True,
        "mode": "one_execution_per_fingerprint",
        "cooldown_minutes_after_close": 30,
        "apply_to_families": ["COMPRESSION_RELEASE", "BREAKOUT_RETEST_CONTINUATION"],
    }
    return config


def _symbol_spec(contract_size: float = 1.0, volume_min: float = 0.1) -> dict[str, float]:
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
    }


def _run_scripted_backtest(
    tmp_path: Path,
    monkeypatch,
    *,
    bars: pd.DataFrame,
    candidates_by_time: dict[str, list[dict[str, object]]],
    execution_model: str = "next_bar_open",
    config_overrides: dict[str, object] | None = None,
    risk_volume: float = 1.0,
    management_actions: dict[str, list[dict[str, object]]] | None = None,
    tracker: dict[str, object] | None = None,
    symbol_spec: dict[str, float] | None = None,
    request_overrides: dict[str, object] | None = None,
    stop_distance: float = 0.20,
    tp1_distance: float = 0.20,
    tp2_distance: float = 0.40,
    risk_amount: float = 50.0,
) -> dict[str, object]:
    config = _base_config()
    if config_overrides:
        config = BacktestRunner._deep_merge_config(config, deepcopy(config_overrides))
    db = DatabaseService(tmp_path / "bot.db")
    spec = symbol_spec or _symbol_spec()
    actions_by_time = management_actions or {}
    visibility = tracker if tracker is not None else {}

    class FakeConnector:
        def __init__(self, _config, _logger):
            self.config = _config

        def initialize(self):
            return True

        def shutdown(self):
            return None

        def get_symbol_spec(self):
            return dict(spec)

    class FakeDecisionEngine:
        def __init__(self, _config):
            pass

        def decide(self, candidate, entry, market_context):
            return SimpleNamespace(
                action="EXECUTE",
                order_type=str(candidate.get("order_type", "market")),
                entry_mode=str(candidate.get("entry_mode", "confirmed")),
                reason_code="entry_valid",
                reason="ok",
                entry_price=float(entry.get("entry_price", 0.0)),
                reference_price=float(entry.get("entry_price", 0.0)),
                pending_expiry_minutes=5,
                pending_expiry_bars=3,
                strategy_family=str(candidate["setup_family"]),
                quality_tier="A",
                management_profile="default",
                metadata={},
            )

    class FakeRiskEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def approve_order(self, candidate, entry, market_context, account_info, symbol_spec, spread_points, live_requested, reduced_risk):
            entry_price = float(entry["entry_price"])
            direction = str(candidate["side"])
            return SimpleNamespace(
                approved=True,
                volume=risk_volume,
                reason_code="risk_approved",
                metadata={
                    "trade_plan": {
                        "direction": direction,
                        "entry_price": entry_price,
                        "stop_loss": entry_price - stop_distance if direction == "LONG" else entry_price + stop_distance,
                        "tp1": entry_price + tp1_distance if direction == "LONG" else entry_price - tp1_distance,
                        "tp2": entry_price + tp2_distance if direction == "LONG" else entry_price - tp2_distance,
                        "management_profile_key": "default",
                    },
                    "sizing": {"risk_amount": risk_amount},
                },
            )

    class FakeRiskManager:
        def __init__(self, config, logger=None):
            self._delegate = RiskManager(config, logger=logger)

        def rebase_trade_levels(self, trade_plan, final_entry_price, symbol_spec):
            rebased = dict(trade_plan)
            entry_price = float(final_entry_price)
            direction = str(trade_plan["direction"])
            rebased["entry_price"] = entry_price
            if direction == "LONG":
                rebased["stop_loss"] = entry_price - stop_distance
                rebased["tp1"] = entry_price + tp1_distance
                rebased["tp2"] = entry_price + tp2_distance
            else:
                rebased["stop_loss"] = entry_price + stop_distance
                rebased["tp1"] = entry_price - tp1_distance
                rebased["tp2"] = entry_price - tp2_distance
            return rebased

        def evaluate_management_actions(self, **kwargs):
            return deepcopy(actions_by_time.get(kwargs["now_utc"].isoformat(), []))

        def fit_volume_to_margin(self, *args, **kwargs):
            return self._delegate.fit_volume_to_margin(*args, **kwargs)

    class ScriptedStrategy:
        def refresh_config(self, *_args, **_kwargs):
            return None

        def prepare_trend_dataframe(self, df):
            return df.copy()

        def prepare_setup_dataframe(self, df):
            prepared = df.copy()
            prepared["atr"] = 1.0
            return prepared

        def prepare_trigger_dataframe(self, df):
            prepared = df.copy()
            prepared["ema_20"] = prepared["close"]
            return prepared

        def analyze_market_context(self, now_utc, trend_df, setup_df, trigger_df, spread_points):
            return {
                "timestamp": now_utc.isoformat(),
                "session": {"session_name": "LONDON", "session_live_allowed": True, "session_quality_score": 90.0},
                "bias": {"direction": "LONG", "confidence": 80.0},
                "regime": {"regime_name": "TREND_CONTINUATION", "regime_confidence": 90.0, "live_allowed": True},
                "spread_points": spread_points,
            }

        def generate_setup_candidates(self, market_context, trend_df, setup_df, trigger_df, symbol_spec):
            key = str(market_context["timestamp"])
            visibility.setdefault("observations", []).append(
                {
                    "bar_time": key,
                    "trend_max": trend_df["time"].max().isoformat(),
                    "setup_max": setup_df["time"].max().isoformat(),
                    "trigger_max": trigger_df["time"].max().isoformat(),
                }
            )
            return deepcopy(candidates_by_time.get(key, []))

        def choose_best_setup(self, candidates):
            return candidates[0] if candidates else None

        def evaluate_entry(self, candidate, trigger_df, symbol_spec, now_utc, live_profile):
            price = float(candidate.get("entry_price") or candidate.get("value_price") or trigger_df.iloc[-1]["close"])
            return {
                "valid": True,
                "live_ready": True,
                "dry_run_ready": True,
                "entry_mode": str(candidate.get("entry_mode", "confirmed")),
                "trigger_type": str(candidate.get("trigger_type", "COMPRESSION_RELEASE")),
                "entry_price": price,
                "pending_entry_price": price,
                "trigger_price": price,
                "reason_code": "entry_valid",
                "reason": "ok",
                "trigger_score": 60.0,
                "entry_score": 70.0,
                "trend_score": 70.0,
                "setup_score": 70.0,
            }

    monkeypatch.setattr("services.backtest_runner.MT5Connector", FakeConnector)
    monkeypatch.setattr("services.backtest_runner.ExecutionDecisionEngine", FakeDecisionEngine)
    monkeypatch.setattr("services.backtest_runner.RiskEngine", FakeRiskEngine)
    monkeypatch.setattr("services.backtest_runner.RiskManager", FakeRiskManager)
    monkeypatch.setattr("services.backtest_runner.build_strategy_engine", lambda *_args, **_kwargs: ScriptedStrategy())

    runner = BacktestRunner(tmp_path, config, db)
    runner.logger.info = lambda *_args, **_kwargs: None
    runner.logger.warning = lambda *_args, **_kwargs: None
    runner.logger.error = lambda *_args, **_kwargs: None
    runner.logger.structured = lambda *_args, **_kwargs: None
    trend_tf = str(config["timeframes"]["trend"])
    setup_tf = str(config["timeframes"]["setup"])
    trigger_tf = str(config["timeframes"]["trigger"])
    resolution = HistoryResolution(
        source_kind="mt5",
        bar_frames={trend_tf: bars.copy(), setup_tf: bars.copy(), trigger_tf: bars.copy()},
        source_details={"source_kind": "mt5"},
    )
    runner.history_loader = SimpleNamespace(resolve_bar_bundle=lambda **_kwargs: resolution)
    start_day = pd.Timestamp(bars.iloc[0]["time"]).date().isoformat()
    end_day = pd.Timestamp(bars.iloc[-1]["time"]).date().isoformat()
    request = {
        "symbol": "XAUUSD",
        "timeframe": "M1",
        "start_date": start_day,
        "end_date": end_day,
        "enabled_strategies": ["XAU_BOT_COMPRESS"],
        "session_filter": {"enabled": False, "allowed_sessions": []},
        "initial_balance": 10000.0,
        "risk_percent": 0.5,
        "spread_model": {"type": "fixed_points", "points": 0.0},
        "slippage_model": {"type": "fixed_points", "points": 0.0},
        "execution_model": execution_model,
        "config_overrides": config_overrides or {},
    }
    if request_overrides:
        request.update(request_overrides)
    return runner.run(request)


def _candidate(ts: pd.Timestamp, fingerprint: str = "fp-1", *, side: str = "LONG") -> dict[str, object]:
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


def _structure_config(min_bars: int, consecutive: int) -> dict[str, object]:
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
            "min_margin_level": 100.0,
            "margin_protection_enabled": True,
            "min_free_margin_buffer": 0.0,
        },
        "exit": {
            "partial_tp_enabled": False,
            "breakeven_after_tp1": False,
            "trailing_enabled": False,
            "momentum_failure_exit_enabled": False,
            "structure_break_exit_enabled": True,
            "structure_break_lookback_bars": 3,
            "structure_break_exit": {
                "enabled": True,
                "min_bars_after_entry": min_bars,
                "require_consecutive_closes": consecutive,
                "atr_buffer_multiplier": 0.0,
                "spread_buffer_points": 0.0,
            },
            "family_profiles": {},
            "max_trade_duration_minutes": 999.0,
            "time_stop_early_minutes": 999.0,
            "time_stop_mid_minutes": 999.0,
            "breakeven_buffer_pips": 0.0,
            "sl_buffer_atr": 0.25,
            "sl_buffer_pips": 1.0,
            "sl_min_pips": 0.5,
            "sl_max_pips": 50.0,
            "partial_close_fraction": 0.5,
        },
    }


def test_no_future_candles_in_signal_generation(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    signal_ts = pd.Timestamp(bars.iloc[55]["time"])
    tracker: dict[str, object] = {}
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts)]},
        tracker=tracker,
    )
    assert bundle["signals"]
    for observation in tracker["observations"]:
        assert observation["trend_max"] <= observation["bar_time"]
        assert observation["setup_max"] <= observation["bar_time"]
        assert observation["trigger_max"] <= observation["bar_time"]


def test_next_bar_open_fill_happens_after_signal_bar(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    signal_ts = pd.Timestamp(bars.iloc[55]["time"])
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts)]},
        execution_model="next_bar_open",
    )
    trade = bundle["trades"][0]
    metadata = trade["trade_metadata_json"]
    assert trade["open_time"] > metadata["signal_bar_time"]
    assert metadata["execution_model"] == "next_bar_open"
    assert metadata["signal_bar_close"] == pytest.approx(float(bars.iloc[55]["close"]))
    assert metadata["data_available_through_time"] == metadata["signal_bar_time"]


def test_structure_break_min_bars_gate() -> None:
    manager = RiskManager(_structure_config(min_bars=5, consecutive=1))
    trigger_df = pd.DataFrame(
        {
            "time": pd.date_range("2026-04-01", periods=4, freq="min", tz="UTC"),
            "high": [101.0, 100.8, 100.7, 100.6],
            "low": [99.2, 99.0, 98.8, 98.6],
            "close": [100.0, 99.1, 98.9, 98.7],
            "ema_20": [100.5, 100.0, 99.6, 99.3],
        }
    )
    setup_df = trigger_df.copy()
    setup_df["atr"] = 1.0
    position = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl": 99.0,
        "tp1": 100.5,
        "tp2": 101.0,
        "volume": 1.0,
        "initial_risk_price": 1.0,
        "management_bars_seen": 3,
        "opened_at": "2026-04-01T00:00:00+00:00",
        "setup_family": "COMPRESSION_RELEASE",
        "regime_at_entry": "TREND_CONTINUATION",
        "entry_score": 80.0,
        "partial_closed": False,
        "breakeven_moved": False,
        "trailing_active": False,
        "highest_price": 100.0,
        "lowest_price": 100.0,
        "mfe": 0.0,
        "mae": 0.0,
    }
    actions = manager.evaluate_management_actions(
        position=position,
        trigger_df=trigger_df,
        setup_df=setup_df,
        current_price=98.7,
        now_utc=pd.Timestamp("2026-04-01T00:04:00+00:00"),
        symbol_spec={"pip_size": 0.1, "point": 0.1, "volume_min": 0.1, "volume_step": 0.1},
    )
    assert not any(action["reason_code"] == "structure_break_exit" for action in actions)
    assert position["last_structure_break_evaluation"]["exit_blocked_reason"] == "min_bars_after_entry"


def test_structure_break_requires_consecutive_closes() -> None:
    manager = RiskManager(_structure_config(min_bars=0, consecutive=2))
    trigger_df = pd.DataFrame(
        {
            "time": pd.date_range("2026-04-01", periods=4, freq="min", tz="UTC"),
            "high": [101.0, 100.8, 100.7, 100.6],
            "low": [99.2, 99.0, 98.8, 98.6],
            "close": [100.0, 99.8, 98.5, 98.4],
            "ema_20": [100.5, 100.0, 99.7, 99.2],
        }
    )
    setup_df = trigger_df.copy()
    setup_df["atr"] = 1.0
    position = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl": 99.0,
        "tp1": 100.5,
        "tp2": 101.0,
        "volume": 1.0,
        "initial_risk_price": 1.0,
        "management_bars_seen": 5,
        "opened_at": "2026-04-01T00:00:00+00:00",
        "setup_family": "COMPRESSION_RELEASE",
        "regime_at_entry": "TREND_CONTINUATION",
        "entry_score": 80.0,
        "partial_closed": False,
        "breakeven_moved": False,
        "trailing_active": False,
        "highest_price": 100.0,
        "lowest_price": 100.0,
        "mfe": 0.0,
        "mae": 0.0,
    }
    first_actions = manager.evaluate_management_actions(
        position=deepcopy(position),
        trigger_df=trigger_df.iloc[:3].copy(),
        setup_df=setup_df.iloc[:3].copy(),
        current_price=99.3,
        now_utc=pd.Timestamp("2026-04-01T00:03:00+00:00"),
        symbol_spec={"pip_size": 0.1, "point": 0.1, "volume_min": 0.1, "volume_step": 0.1},
    )
    second_actions = manager.evaluate_management_actions(
        position=deepcopy(position),
        trigger_df=trigger_df.copy(),
        setup_df=setup_df.copy(),
        current_price=98.7,
        now_utc=pd.Timestamp("2026-04-01T00:04:00+00:00"),
        symbol_spec={"pip_size": 0.1, "point": 0.1, "volume_min": 0.1, "volume_step": 0.1},
    )
    assert not any(action["reason_code"] == "structure_break_exit" for action in first_actions)
    assert any(action["reason_code"] == "structure_break_exit" for action in second_actions)


def test_partial_tp_not_double_counted() -> None:
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0)
    trade = executor.open_trade(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-04-01T00:00:00+00:00",
        entry=100.0,
        sl=99.0,
        tp=102.0,
        volume=1.0,
        setup="COMPRESSION_RELEASE",
        metadata={"setup_fingerprint": "fp-pt"},
        tp1=101.0,
    )
    partial = executor.apply_management_actions(
        trade,
        [{"action": "partial_close", "volume": 0.5, "reason_code": "partial_tp1"}],
        101.0,
        "2026-04-01T00:01:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )[0]
    balance = 10000.0 + float(partial["pnl"])
    closed = executor.close_trade_now(
        trade=trade,
        exit_price=102.0,
        exit_reason="tp_hit",
        close_time="2026-04-01T00:02:00+00:00",
        reason_code="tp_hit",
    )
    balance += float(closed["final_realized_pnl"])
    assert partial["pnl"] == pytest.approx(0.5)
    assert closed["partial_realized_pnl"] == pytest.approx(0.5)
    assert closed["final_realized_pnl"] == pytest.approx(1.0)
    assert closed["realized_pnl_total"] == pytest.approx(1.5)
    assert balance == pytest.approx(10001.5)


def test_tp_sl_trade_closed_events_are_emitted(tmp_path: Path, monkeypatch) -> None:
    for close_mode in ("tp", "sl"):
        bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
        signal_ts = pd.Timestamp(bars.iloc[55]["time"])
        if close_mode == "tp":
            bars.loc[57, "high"] = bars.loc[56, "open"] + 1.0
        else:
            bars.loc[57, "low"] = bars.loc[56, "open"] - 1.0
        bundle = _run_scripted_backtest(
            tmp_path / close_mode,
            monkeypatch,
            bars=bars,
            candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts, fingerprint=f"fp-{close_mode}")]},
        )
        event_types = [row["event_type"] for row in bundle["playback_events"]]
        close_reasons = [row["event_payload_json"].get("reason_code") for row in bundle["playback_events"] if row["event_type"] == "trade_closed"]
        assert "trade_closed" in event_types
        assert ("tp_hit" if close_mode == "tp" else "sl_hit") in close_reasons


def test_summary_trades_playback_reconcile(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    signal_ts = pd.Timestamp(bars.iloc[55]["time"])
    bars.loc[57, "high"] = bars.loc[56, "open"] + 1.0
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts)]},
    )
    summary = bundle["summary"]
    assert summary["reconciliation_status"] == "ok"
    assert summary["final_balance"] == pytest.approx(summary["initial_balance"] + summary["net_profit"])
    assert summary["frame_final_balance"] == pytest.approx(summary["final_balance"])
    assert summary["trade_closed_events"] == bundle["summary"]["total_trades"]


def test_account_blown_stops_backtest(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    first_signal = pd.Timestamp(bars.iloc[55]["time"])
    second_signal = pd.Timestamp(bars.iloc[58]["time"])
    bars.loc[57, "low"] = bars.loc[56, "open"] - 1.0
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={
            first_signal.isoformat(): [_candidate(first_signal, fingerprint="fp-loss-1")],
            second_signal.isoformat(): [_candidate(second_signal, fingerprint="fp-loss-2")],
        },
        config_overrides={"risk": {"backtest_account_protection": {"enabled": True, "max_consecutive_losses": 1, "enforce_margin": True}}},
    )
    assert bundle["summary"]["account_status"] == "PROTECTION_STOPPED"
    assert bundle["summary"]["total_trades"] == 1


def test_insufficient_margin_rejects_trade(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    signal_ts = pd.Timestamp(bars.iloc[55]["time"])
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts)]},
        risk_volume=1.0,
        risk_amount=0.5,
        config_overrides={"risk": {"backtest_account_protection": {"enabled": True, "enforce_margin": True}}},
        symbol_spec=_symbol_spec(contract_size=1000.0, volume_min=1.0),
        request_overrides={"initial_balance": 100.0, "leverage": 1.0},
    )
    assert bundle["signals"][0]["blocked_reason"] == "insufficient_margin"
    assert bundle["summary"]["rejected_insufficient_margin_count"] == 1


def test_friday_hard_close_closes_positions(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 3, 19, 30, tzinfo=timezone.utc), periods=100)
    signal_ts = pd.Timestamp(bars.iloc[54]["time"])
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts, fingerprint="fp-friday")]},
        config_overrides={"risk": {"weekend_protection": {"enabled": True, "friday_hard_close_time_utc": "21:00", "block_new_entries_after_utc": "20:30", "cancel_pending_orders": True, "close_open_positions": True}}},
        stop_distance=5.0,
        tp1_distance=5.0,
        tp2_distance=5.0,
    )
    event_types = [row["event_type"] for row in bundle["playback_events"]]
    close_reasons = [row["event_payload_json"].get("reason_code") for row in bundle["playback_events"] if row["event_type"] == "forced_close"]
    assert "forced_close" in event_types
    assert "friday_hard_close" in close_reasons


def test_weekend_cutoff_blocks_new_entries(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 3, 19, 30, tzinfo=timezone.utc), periods=100)
    signal_ts = pd.Timestamp(bars.iloc[65]["time"])
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts, fingerprint="fp-cutoff")]},
        config_overrides={"risk": {"weekend_protection": {"enabled": True, "friday_hard_close_time_utc": "21:00", "block_new_entries_after_utc": "20:30", "cancel_pending_orders": True, "close_open_positions": True}}},
    )
    assert bundle["signals"][0]["blocked_reason"] == "blocked_weekend_cutoff"


def test_duplicate_setup_fingerprint_blocked(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 1, tzinfo=timezone.utc))
    first_signal = pd.Timestamp(bars.iloc[55]["time"])
    second_signal = pd.Timestamp(bars.iloc[58]["time"])
    bars.loc[57, "high"] = bars.loc[56, "open"] + 1.0
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={
            first_signal.isoformat(): [_candidate(first_signal, fingerprint="fp-dup")],
            second_signal.isoformat(): [_candidate(second_signal, fingerprint="fp-dup")],
        },
    )
    blocked = [row for row in bundle["signals"] if row.get("blocked_reason") == "blocked_duplicate_setup_fingerprint"]
    assert blocked


def test_requested_vs_effective_data_range_recorded(tmp_path: Path, monkeypatch) -> None:
    bars = _bars(datetime(2026, 4, 3, tzinfo=timezone.utc))
    signal_ts = pd.Timestamp(bars.iloc[55]["time"])
    bundle = _run_scripted_backtest(
        tmp_path,
        monkeypatch,
        bars=bars,
        candidates_by_time={signal_ts.isoformat(): [_candidate(signal_ts)]},
        config_overrides={},
        request_overrides={"end_date": "2026-04-05"},
    )
    summary = bundle["summary"]
    assert summary["requested_end"].startswith("2026-04-05")
    assert summary["effective_data_end"] < summary["requested_end"]
    assert summary["data_truncated_reason"] == "requested_end_after_available_data"


def test_equity_curve_not_silently_truncated(tmp_path: Path) -> None:
    db = DatabaseService(tmp_path / "bot.db")
    run_id = db.create_backtest_run(
        {
            "created_at": "2026-04-01T00:00:00+00:00",
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
            "enabled_strategies_json": ["XAU_BOT_COMPRESS"],
            "session_filter_json": {"enabled": False},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 0.0},
            "slippage_model": {"type": "fixed_points", "points": 0.0},
            "execution_model": "next_bar_open",
            "notes": "equity export",
            "state": "COMPLETED",
        }
    )
    with db._connect() as connection:
        connection.executemany(
            """
            INSERT INTO backtest_equity_curve (run_id, ts, equity, balance, floating_pnl)
            VALUES (?, ?, ?, ?, ?);
            """,
            [
                (
                    run_id,
                    f"2026-04-01T00:{index % 60:02d}:00+00:00",
                    10000.0 + index,
                    10000.0 + index,
                    0.0,
                )
                for index in range(25050)
            ],
        )
    storage = BacktestStorage(db)
    bundle = storage.get_run_bundle(run_id)
    assert len(bundle["equity_curve"]) == 25050
    assert bundle["summary"]["exported_frame_count"] == 25050
    assert bundle["equity_curve"][-1]["balance"] == pytest.approx(35049.0)


def test_live_backtest_shared_execution_plan_parity() -> None:
    plan = CanonicalExecutionPlan(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        order_type="market",
        entry_price=100.5,
        volume=0.5,
        stop_loss=100.0,
        take_profit=101.3,
        setup_family="COMPRESSION_RELEASE",
        setup_fingerprint="fp-parity",
        entry_mode="confirmed",
        execution_model="next_bar_open",
        signal_time="2026-04-01T00:55:00+00:00",
        execution_time="2026-04-01T00:56:00+00:00",
        signal_bar_time="2026-04-01T00:55:00+00:00",
        signal_bar_close=100.4,
        execution_bar_time="2026-04-01T00:56:00+00:00",
        executable_entry=100.5,
        spread_used=0.0,
        slippage_used=0.0,
        fill_side="ask",
        data_available_through_time="2026-04-01T00:55:00+00:00",
        tp1=100.9,
        trigger_price=100.4,
    )
    live_request = build_execution_request_from_plan(plan, extra_metadata={"runtime": "live"})
    backtest_request = build_execution_request_from_plan(plan, extra_metadata={"runtime": "backtest"})
    assert live_request.strategy_name == backtest_request.strategy_name
    assert live_request.entry_price == pytest.approx(backtest_request.entry_price)
    assert live_request.stop_loss == pytest.approx(backtest_request.stop_loss)
    assert live_request.take_profit == pytest.approx(backtest_request.take_profit)
    assert live_request.metadata["signal_bar_time"] == backtest_request.metadata["signal_bar_time"]
    assert live_request.metadata["data_available_through_time"] == backtest_request.metadata["data_available_through_time"]
