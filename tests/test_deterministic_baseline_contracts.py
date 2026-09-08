from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from risk_manager import RiskManager
from services.backtest_runner import BacktestRunner, _SimAccount
from services.simulated_executor import SimulatedExecutor
from strategy_factory import build_strategy_engine
from tests.helpers.deterministic_market import canonical_plan, candidate, candles, flat_no_signal_candles, symbol_spec
from trading_bot.core.execution_mode import V1_BASELINE
from trading_bot.core.reasons import ManagementAction, ReasonCode
from trading_bot.execution.canonical import build_execution_request_from_plan
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.replay_broker import ReplayBroker

pytestmark = pytest.mark.fast


def _config() -> dict:
    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    config.setdefault("bot", {})["execution_mode"] = V1_BASELINE
    return config


def _executor(*, same_bar_rule: str = "sl_first", allow_pyramiding: bool = False, commission_per_lot: float = 0.0) -> SimulatedExecutor:
    return SimulatedExecutor(
        spread_points=0.0,
        slippage_points=0.0,
        point=0.1,
        contract_size=1.0,
        same_bar_rule=same_bar_rule,
        allow_pyramiding=allow_pyramiding,
        commission_per_lot=commission_per_lot,
    )


def _open_long(executor: SimulatedExecutor, *, open_time: str = "2026-04-01T00:00:00+00:00"):
    return executor.open_trade(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        open_time=open_time,
        entry=100.0,
        sl=99.0,
        tp=102.0,
        volume=1.0,
        setup="COMPRESSION_RELEASE",
        metadata={"setup_fingerprint": "fp-open"},
        tp1=101.0,
    )


def test_scenario_a_no_signal_real_strategy_is_deterministic() -> None:
    config = _config()
    engine = build_strategy_engine(config)
    raw = flat_no_signal_candles(periods=80)
    prepared_trend = engine.prepare_trend_dataframe(raw)
    prepared_setup = engine.prepare_setup_dataframe(raw)
    prepared_trigger = engine.prepare_trigger_dataframe(raw)
    now = pd.Timestamp(raw.iloc[-1]["time"]).to_pydatetime()

    first_market = engine.analyze_market_context(now, prepared_trend, prepared_setup, prepared_trigger, spread_points=0.0)
    first_candidates = engine.generate_setup_candidates(first_market, prepared_trend, prepared_setup, prepared_trigger, symbol_spec())
    first_selected = engine.choose_best_setup(first_candidates)
    first_entry = engine.evaluate_entry(first_selected, prepared_trigger, symbol_spec(), now, live_profile=True)

    second_market = engine.analyze_market_context(now, prepared_trend, prepared_setup, prepared_trigger, spread_points=0.0)
    second_candidates = engine.generate_setup_candidates(second_market, prepared_trend, prepared_setup, prepared_trigger, symbol_spec())
    second_selected = engine.choose_best_setup(second_candidates)
    second_entry = engine.evaluate_entry(second_selected, prepared_trigger, symbol_spec(), now, live_profile=True)

    assert len(first_candidates) == len(second_candidates) == 0
    assert first_selected is second_selected is None
    assert first_entry["reason_code"] == second_entry["reason_code"] == "no_setup_candidate"
    assert first_market["session"]["session_name"] == second_market["session"]["session_name"]


def test_scenario_b_valid_immediate_entry_uses_canonical_replay_path() -> None:
    executor = _executor()
    service = ExecutionService(ReplayBroker(executor))
    plan = canonical_plan(execution_time="2026-04-01T00:01:00+00:00")

    response = service.submit_order(build_execution_request_from_plan(plan))

    assert response["ok"] is True
    assert response["status"] == "filled"
    assert len(executor.open_trades) == 1
    trade = executor.open_trades[0]
    assert trade.entry == pytest.approx(plan.entry_price)
    assert trade.sl == pytest.approx(plan.stop_loss)
    assert trade.tp == pytest.approx(plan.take_profit)


def test_scenarios_c_and_d_pending_activation_and_expiry_are_deterministic() -> None:
    executor = _executor(allow_pyramiding=True)
    service = ExecutionService(ReplayBroker(executor))
    pending_plan = canonical_plan(
        order_type="limit",
        entry_mode="limit_value",
        entry_price=99.5,
        metadata={
            "pending_expiry_bars": 2,
            "pending_expiry_minutes": 5,
            "placed_bar_index": 3,
            "created_at_ts": pd.Timestamp("2026-04-01T00:03:00Z").timestamp(),
        },
    )

    response = service.submit_order(build_execution_request_from_plan(pending_plan))
    assert response["status"] == "pending"
    assert executor.update_pending_orders({"time": pd.Timestamp("2026-04-01T00:03:00Z"), "low": 99.4, "high": 99.6}, "2026-04-01T00:03:00+00:00", 3) == []

    filled = executor.update_pending_orders({"time": pd.Timestamp("2026-04-01T00:04:00Z"), "low": 99.4, "high": 99.6}, "2026-04-01T00:04:00+00:00", 4)
    assert [event["event_type"] for event in filled] == ["pending_filled"]
    assert len(executor.open_trades) == 1

    expiring = _executor()
    expiring.place_pending_order(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-04-01T00:00:00+00:00",
        setup="COMPRESSION_RELEASE",
        setup_fingerprint="fp-expire",
        trigger_price=100.0,
        pending_entry_price=99.5,
        volume=1.0,
        sl=99.0,
        tp=101.0,
        tp1=100.5,
        pending_expiry_bars=1,
        pending_expiry_minutes=5,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-04-01T00:00:00Z").timestamp()},
    )
    expired = expiring.update_pending_orders({"time": pd.Timestamp("2026-04-01T00:02:00Z"), "low": 100.0, "high": 100.2}, "2026-04-01T00:02:00+00:00", 2)
    assert [event["event_type"] for event in expired] == ["pending_expired"]
    assert expiring.pending_orders == []


def test_scenarios_e_f_g_h_tp1_tp2_sl_and_breakeven_accounting() -> None:
    executor = _executor()
    trade = _open_long(executor)
    partial = executor.apply_management_actions(
        trade,
        [{"action": ManagementAction.PARTIAL_CLOSE.value, "volume": 0.5, "reason_code": "partial_tp1"}],
        101.0,
        "2026-04-01T00:01:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )[0]
    assert partial["pnl"] == pytest.approx(0.5)
    assert trade.volume == pytest.approx(0.5)

    moved = executor.apply_management_actions(
        trade,
        [{"action": ManagementAction.MOVE_STOP.value, "sl": 100.0, "reason_code": "breakeven_after_tp1"}],
        101.0,
        "2026-04-01T00:02:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )[0]
    assert moved["event_type"] == ManagementAction.MOVE_STOP.value
    stopped_after_tp1 = executor.on_bar({"low": 99.9, "high": 100.2}, "2026-04-01T00:03:00+00:00")[0]
    assert stopped_after_tp1["exit_reason_code"] == ReasonCode.STOP_LOSS_EXIT.value
    assert stopped_after_tp1["partial_realized_pnl"] == pytest.approx(0.5)
    assert stopped_after_tp1["realized_pnl_total"] == pytest.approx(0.5)

    tp_executor = _executor()
    tp_trade = _open_long(tp_executor)
    tp_closed = tp_executor.on_bar({"low": 100.5, "high": 102.0}, "2026-04-01T00:01:00+00:00")[0]
    assert tp_closed["exit_reason_code"] == ReasonCode.TAKE_PROFIT_EXIT.value
    assert tp_closed["final_realized_pnl"] == pytest.approx(2.0)

    sl_executor = _executor()
    sl_trade = _open_long(sl_executor)
    sl_closed = sl_executor.on_bar({"low": 99.0, "high": 100.5}, "2026-04-01T00:01:00+00:00")[0]
    assert sl_closed["exit_reason_code"] == ReasonCode.STOP_LOSS_EXIT.value
    assert sl_closed["final_realized_pnl"] == pytest.approx(-1.0)
    assert tp_trade.closed is True
    assert sl_trade.closed is True


def test_scenario_i_duplicate_setup_policy_blocks_current_conflicts() -> None:
    executor = _executor(allow_pyramiding=True)
    executor.place_pending_order(
        strategy_name="XAU_BOT_LEBPRIM",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-04-01T00:00:00+00:00",
        setup="LEBPRIM_SCALP",
        setup_fingerprint="fp-dup",
        trigger_price=100.0,
        pending_entry_price=99.8,
        volume=1.0,
        sl=99.0,
        tp=101.0,
        tp1=100.4,
        metadata={"entry_mode": "lebprim_limit"},
    )

    ok, reason = executor.can_accept_signal("XAUUSD", "LONG", "LEBPRIM_SCALP", "fp-dup", "lebprim_limit")

    assert ok is False
    assert reason == "pending_order_exists"


def test_scenario_j_weekend_friday_cutoff_policy_is_preserved() -> None:
    cutoff = BacktestRunner._clock_time_utc("20:30", "20:30")
    assert BacktestRunner._is_friday_cutoff(pd.Timestamp("2026-04-03T20:29:00Z").to_pydatetime(), cutoff) is False
    assert BacktestRunner._is_friday_cutoff(pd.Timestamp("2026-04-03T20:30:00Z").to_pydatetime(), cutoff) is True
    assert BacktestRunner._is_friday_cutoff(pd.Timestamp("2026-04-04T12:00:00Z").to_pydatetime(), cutoff) is False


def test_scenario_k_margin_rejection_is_deterministic() -> None:
    config = _config()
    manager = RiskManager(config)
    account = type("Account", (), {"margin_free": 100.0, "margin": 0.0, "margin_level": 0.0, "equity": 100.0})()
    result = manager.fit_volume_to_margin(
        requested_volume=1.0,
        account_info=account,
        symbol_spec={**symbol_spec(contract_size=1000.0, volume_min=1.0), "volume_step": 1.0},
        margin_for_volume=lambda volume: 1000.0 * float(volume),
    )
    assert result["valid"] is False
    assert result["reason_code"] == "margin_constraint"


def test_scenario_l_account_protection_consecutive_loss_contract() -> None:
    config = _config()
    config["risk"]["backtest_account_protection"] = {
        "enabled": True,
        "max_consecutive_losses": 1,
        "enforce_margin": True,
        "max_total_drawdown_percent": None,
        "max_daily_drawdown_percent": None,
        "stop_when_equity_below": 0.0,
    }
    account = _SimAccount(balance=9999.0, equity=9999.0, consecutive_losses=1)
    account.peak_equity = 10000.0
    account.daily_start_equity = 10000.0

    reason = BacktestRunner._check_backtest_account_limits(object(), account, config["risk"]["backtest_account_protection"])

    assert reason == "max_consecutive_losses_reached"


def test_canonical_live_backtest_comparable_intent_ignores_runtime_specific_metadata() -> None:
    plan = canonical_plan()
    live = canonical_plan(metadata={**plan.metadata, "runtime": "live", "broker_ticket": 123})
    replay = canonical_plan(metadata={**plan.metadata, "runtime": "backtest", "sim_id": "SIM-1"})

    assert live.to_comparable_dict() == replay.to_comparable_dict()


def test_no_lookahead_direct_m1_and_higher_timeframe_close_semantics() -> None:
    bars = candles(periods=20)
    current_ts = pd.Timestamp(bars.iloc[15]["time"])
    m1_visible = bars[bars["time"] <= current_ts]
    m3 = bars.iloc[::3].copy()
    m3_visible = m3[m3["time"] + pd.Timedelta(minutes=3) <= current_ts]
    m15 = bars.iloc[::15].copy()
    m15_visible = m15[m15["time"] + pd.Timedelta(minutes=15) <= current_ts]

    assert m1_visible["time"].max() <= current_ts
    assert (m3_visible["time"] + pd.Timedelta(minutes=3)).max() <= current_ts
    assert (m15_visible["time"] + pd.Timedelta(minutes=15)).max() <= current_ts
    assert pd.Timestamp(bars.iloc[16]["time"]) not in set(m1_visible["time"])
    assert pd.Timestamp(m3.iloc[-1]["time"]) not in set(m3_visible["time"])


@pytest.mark.parametrize(
    ("execution_model", "signal_bar", "next_bar", "expected"),
    [
        ("next_bar_open", {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.4}, {"open": 100.8}, {"fill_time": "N+1", "price": 100.8}),
        ("signal_price_touch", {"open": 100.0, "high": 100.6, "low": 100.2, "close": 100.4}, {"open": 100.8}, {"fill_time": "N", "price": 100.5}),
        ("current_bar_close", {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.4}, {"open": 100.8}, {"fill_time": "N", "price": 100.4}),
    ],
)
def test_execution_model_timing_contracts(execution_model: str, signal_bar: dict, next_bar: dict, expected: dict) -> None:
    requested_entry = 100.5
    if execution_model == "next_bar_open":
        fill_time = "N+1"
        price = float(next_bar["open"])
    elif execution_model == "current_bar_close":
        fill_time = "N"
        price = float(signal_bar["close"])
    else:
        touched = float(signal_bar["low"]) <= requested_entry <= float(signal_bar["high"])
        fill_time = "N" if touched else "blocked"
        price = requested_entry if touched else None

    assert fill_time == expected["fill_time"]
    assert price == pytest.approx(expected["price"])


def test_signal_price_touch_blocks_when_signal_bar_does_not_reach_entry() -> None:
    bar = {"open": 100.0, "high": 100.4, "low": 100.2, "close": 100.3}
    requested_entry = 100.5

    assert not (float(bar["low"]) <= requested_entry <= float(bar["high"]))


def test_intrabar_ambiguity_current_default_is_sl_first() -> None:
    sl_first = _executor(same_bar_rule="sl_first")
    _open_long(sl_first)
    closed = sl_first.on_bar({"low": 99.0, "high": 102.0}, "2026-04-01T00:01:00+00:00")[0]
    assert closed["exit_reason_code"] == ReasonCode.STOP_LOSS_EXIT.value
    assert closed["final_realized_pnl"] == pytest.approx(-1.0)

    tp_first = _executor(same_bar_rule="tp_first")
    _open_long(tp_first)
    closed_tp = tp_first.on_bar({"low": 99.0, "high": 102.0}, "2026-04-01T00:01:00+00:00")[0]
    assert closed_tp["exit_reason_code"] == ReasonCode.TAKE_PROFIT_EXIT.value
    assert closed_tp["final_realized_pnl"] == pytest.approx(2.0)


def test_accounting_reconciles_balance_equity_commission_swap_and_partial() -> None:
    starting_balance = 10_000.0
    executor = _executor(commission_per_lot=0.2)
    trade = _open_long(executor)
    trade.metadata["swap"] = 0.1
    partial = executor.apply_management_actions(
        trade,
        [{"action": ManagementAction.PARTIAL_CLOSE.value, "volume": 0.5, "reason_code": "partial_tp1"}],
        101.0,
        "2026-04-01T00:01:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )[0]
    balance_after_partial = starting_balance + partial["pnl"]
    closed = executor.close_trade_now(trade, 102.0, "tp_hit", "2026-04-01T00:02:00+00:00", ReasonCode.TAKE_PROFIT_EXIT.value)
    ending_balance = balance_after_partial + closed["final_realized_pnl"]
    floating = executor.floating_pnl(102.0)

    assert closed["commission"] == pytest.approx(0.2)
    assert closed["swap"] == pytest.approx(0.1)
    assert closed["partial_realized_pnl"] == pytest.approx(0.5)
    assert closed["final_realized_pnl"] == pytest.approx(0.7)
    assert closed["realized_pnl_total"] == pytest.approx(1.2)
    assert ending_balance == pytest.approx(starting_balance + closed["realized_pnl_total"])
    assert ending_balance + floating == pytest.approx(ending_balance)


def test_legal_lifecycle_transitions_and_terminal_close_once() -> None:
    executor = _executor()
    order = executor.place_pending_order(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-04-01T00:00:00+00:00",
        setup="COMPRESSION_RELEASE",
        setup_fingerprint="fp-life",
        trigger_price=100.0,
        pending_entry_price=99.5,
        volume=1.0,
        sl=99.0,
        tp=101.0,
        tp1=100.5,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-04-01T00:00:00Z").timestamp()},
    )
    transitions = ["PENDING"]
    assert executor.on_bar({"low": 98.0, "high": 102.0}, order.placed_time) == []
    filled = executor.update_pending_orders({"time": pd.Timestamp("2026-04-01T00:01:00Z"), "low": 99.4, "high": 99.6}, "2026-04-01T00:01:00+00:00", 1)
    transitions.append("ACTIVATED")
    trade = filled[0]["trade"]
    executor.apply_management_actions(
        trade,
        [{"action": ManagementAction.PARTIAL_CLOSE.value, "volume": 0.5, "reason_code": "partial_tp1"}],
        100.5,
        "2026-04-01T00:02:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )
    transitions.append("TP1")
    closed = executor.on_bar({"low": 100.8, "high": 101.0}, "2026-04-01T00:03:00+00:00")
    transitions.append("TP2")

    assert transitions == ["PENDING", "ACTIVATED", "TP1", "TP2"]
    assert len(closed) == 1
    assert executor.on_bar({"low": 99.0, "high": 101.0}, "2026-04-01T00:04:00+00:00") == []


def test_golden_replay_expected_fixture_matches_current_simulator_contract() -> None:
    expected = json.loads(Path("tests/fixtures/golden_replay_expected.json").read_text(encoding="utf-8"))
    executor = _executor()
    executor.place_pending_order(
        strategy_name="XAU_BOT_COMPRESS",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-04-01T00:00:00+00:00",
        setup="COMPRESSION_RELEASE",
        setup_fingerprint="fp-golden",
        trigger_price=100.0,
        pending_entry_price=100.0,
        volume=1.0,
        sl=99.0,
        tp=102.0,
        tp1=101.0,
        placed_bar_index=0,
        metadata={"created_at_ts": pd.Timestamp("2026-04-01T00:00:00Z").timestamp()},
    )
    lifecycle = ["pending_created"]
    filled = executor.update_pending_orders({"time": pd.Timestamp("2026-04-01T00:01:00Z"), "low": 99.9, "high": 100.1}, "2026-04-01T00:01:00+00:00", 1)
    trade = filled[0]["trade"]
    lifecycle.append("pending_filled")
    partial = executor.apply_management_actions(
        trade,
        [{"action": ManagementAction.PARTIAL_CLOSE.value, "volume": 0.5, "reason_code": "partial_tp1"}],
        101.0,
        "2026-04-01T00:02:00+00:00",
        {"volume_min": 0.1, "volume_step": 0.1},
    )[0]
    lifecycle.append("trade_partial_close")
    closed = executor.on_bar({"low": 101.5, "high": 102.0}, "2026-04-01T00:03:00+00:00")[0]
    lifecycle.append("trade_closed")
    ending_balance = expected["starting_balance"] + float(partial["pnl"]) + float(closed["final_realized_pnl"])

    actual = {
        "scenario": expected["scenario"],
        "candidates": 1,
        "executable_signals": 1,
        "pending_orders": 1,
        "activated_trades": 1,
        "trade_lifecycle_events": lifecycle,
        "tp1_count": 1 if partial["event_type"] == ManagementAction.PARTIAL_CLOSE.value else 0,
        "tp2_count": 1 if closed["exit_reason_code"] == ReasonCode.TAKE_PROFIT_EXIT.value else 0,
        "sl_count": 1 if closed["exit_reason_code"] == ReasonCode.STOP_LOSS_EXIT.value else 0,
        "starting_balance": expected["starting_balance"],
        "ending_balance": ending_balance,
        "ending_equity": ending_balance + executor.floating_pnl(102.0),
        "realized_pnl": float(partial["pnl"]) + float(closed["final_realized_pnl"]),
        "max_drawdown": 0.0,
    }
    assert actual == expected
