from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from risk_manager import RiskManager
from services.simulated_executor import SimulatedExecutor
from trading_bot.core.execution_mode import V2_FULL, apply_execution_mode_to_config
from trading_bot.execution.decision_engine import BLOCK, PLACE_LIMIT, WAIT_RETEST, ExecutionDecisionEngine
from trading_bot.execution.exposure_policy import ExposurePolicy, ExposureSnapshot
from trading_bot.execution.pending_policy import PendingOrderPolicy


def _config() -> dict:
    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    config["bot"]["execution_mode"] = V2_FULL
    config, _ = apply_execution_mode_to_config(config, V2_FULL)
    return config


def _candidate(setup_family: str) -> dict:
    return {
        "setup_family": setup_family,
        "side": "LONG",
        "setup_score": 72.0,
        "trend_score": 68.0,
        "regime_name": "TREND_CONTINUATION",
        "regime_confidence": 86.0,
        "session_name": "LONDON",
        "session_quality_score": 92.0,
        "value_price": 100.0,
        "structure_level": 100.1,
        "atr_at_setup": 1.0,
        "setup_fingerprint": f"fp-{setup_family}",
        "trigger_type": setup_family,
        "strategy_control": {},
        "anchor_time": "2026-01-01T00:00:00+00:00",
    }


def _entry() -> dict:
    return {
        "valid": False,
        "live_ready": False,
        "entry_mode": "observation_only",
        "reason_code": "blocked_chasing_entry",
        "reason": "blocked_chasing_entry",
        "blocked_reasons": ["blocked_chasing_entry"],
        "trigger_score": 61.0,
        "entry_score": 69.0,
        "trend_score": 68.0,
        "setup_score": 72.0,
        "trigger_candle_atr": 1.05,
        "value_distance_atr": 1.1,
        "entry_price": 101.1,
        "pending_entry_price": 100.0,
    }


def _symbol_spec() -> dict:
    return {
        "digits": 2,
        "pip_size": 0.1,
        "point": 0.01,
        "contract_size": 1.0,
        "pip_value_per_lot": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
    }


def test_breakout_extended_signal_becomes_wait_retest() -> None:
    engine = ExecutionDecisionEngine(_config())
    decision = engine.decide(
        candidate=_candidate("BREAKOUT_RETEST_CONTINUATION"),
        entry=_entry(),
        market_context={"regime": {"regime_name": "TREND_CONTINUATION"}},
    )
    assert decision.action == WAIT_RETEST
    assert decision.reason_code == "deferred_wait_retest"
    assert decision.order_type == "limit"


def test_compress_extended_signal_becomes_limit_entry() -> None:
    engine = ExecutionDecisionEngine(_config())
    decision = engine.decide(
        candidate=_candidate("COMPRESSION_RELEASE"),
        entry=_entry(),
        market_context={"regime": {"regime_name": "TREND_CONTINUATION"}},
    )
    assert decision.action == PLACE_LIMIT
    assert decision.reason_code == "deferred_limit_entry"


def test_lebprim_mode_off_blocks_all_entries() -> None:
    config = _config()
    config["strategy"]["lebprim_mode"] = "off"
    engine = ExecutionDecisionEngine(config)
    decision = engine.decide(
        candidate=_candidate("LEBPRIM_SCALP"),
        entry={**_entry(), "blocked_reasons": [], "valid": True, "live_ready": True, "entry_mode": "lebprim_limit"},
        market_context={"regime": {"regime_name": "TREND_CONTINUATION"}},
    )
    assert decision.action == BLOCK
    assert decision.reason_code == "blocked_lebprim_disabled"


def test_exposure_policy_blocks_same_family_stack_when_disabled() -> None:
    config = _config()
    config["execution"]["allow_multi_position"] = True
    policy = ExposurePolicy(config)
    decision = policy.evaluate(
        candidate=_candidate("BREAKOUT_RETEST_CONTINUATION"),
        projected_risk_pct=0.5,
        open_exposures=[
            ExposureSnapshot(
                position_id="1",
                symbol="XAUUSD",
                side="LONG",
                setup_family="BREAKOUT_RETEST_CONTINUATION",
                strategy_family="BREAKOUT",
                setup_fingerprint="existing",
                risk_pct=0.5,
            )
        ],
        pending_exposures=[],
    )
    assert decision.allowed is False
    assert decision.reason_code == "blocked_same_family_cap"


def test_exposure_policy_allows_second_diverse_position_within_caps() -> None:
    config = _config()
    config["execution"]["allow_multi_position"] = True
    config["execution"]["allow_same_direction_multi_strategy"] = True
    policy = ExposurePolicy(config)
    decision = policy.evaluate(
        candidate=_candidate("COMPRESSION_RELEASE"),
        projected_risk_pct=0.4,
        open_exposures=[
            ExposureSnapshot(
                position_id="1",
                symbol="XAUUSD",
                side="LONG",
                setup_family="BREAKOUT_RETEST_CONTINUATION",
                strategy_family="BREAKOUT",
                setup_fingerprint="existing",
                risk_pct=0.5,
            )
        ],
        pending_exposures=[],
    )
    assert decision.allowed is True


def test_family_profiles_change_tp_ambition_between_breakout_and_lebprim() -> None:
    risk_manager = RiskManager(_config())
    breakout_plan = risk_manager.calculate_trade_levels(
        _candidate("BREAKOUT_RETEST_CONTINUATION"),
        100.0,
        _symbol_spec(),
        {"entry_score": 72.0},
        {"regime": {"regime_name": "TREND_CONTINUATION"}},
    )
    lebprim_plan = risk_manager.calculate_trade_levels(
        _candidate("LEBPRIM_SCALP"),
        100.0,
        _symbol_spec(),
        {"entry_score": 72.0},
        {"regime": {"regime_name": "TREND_CONTINUATION"}},
    )
    breakout_tp_distance = breakout_plan["tp2"] - breakout_plan["entry_price"]
    lebprim_tp_distance = lebprim_plan["tp2"] - lebprim_plan["entry_price"]
    assert breakout_tp_distance > lebprim_tp_distance
    assert breakout_plan["management_profile"] == "BreakoutManagementProfile"
    assert lebprim_plan["management_profile"] == "LebprimManagementProfile"


def test_pending_policy_cancels_degraded_lebprim_limit() -> None:
    policy = PendingOrderPolicy(_config())
    decision = policy.assess(
        {
            "setup_family": "LEBPRIM_SCALP",
            "direction": "LONG",
            "entry_price": 100.0,
            "candidate": {"structure_level": 100.0, "atr_at_setup": 1.0, "regime_name": "TREND_CONTINUATION"},
            "entry_assessment": {"entry_score": 65.0},
        },
        {
            "session": {"session_live_allowed": True},
            "regime": {"regime_name": "TREND_CONTINUATION"},
        },
        {"close": 99.7},
    )
    assert decision.action == "cancel"
    assert decision.reason_code == "pending_structure_degraded"


def test_wait_retest_pending_orders_are_treated_as_conflicts() -> None:
    executor = SimulatedExecutor(spread_points=0.0, slippage_points=0.0, point=0.1, contract_size=1.0, allow_pyramiding=True)
    executor.place_pending_order(
        strategy_name="XAU_BOT_BREAKOUT",
        symbol="XAUUSD",
        side="LONG",
        open_time="2026-01-01T00:00:00+00:00",
        setup="BREAKOUT_RETEST_CONTINUATION",
        setup_fingerprint="fp-breakout",
        trigger_price=100.5,
        pending_entry_price=100.0,
        volume=1.0,
        sl=99.0,
        tp=102.0,
        tp1=101.0,
        placed_bar_index=0,
        metadata={"entry_mode": "wait_retest", "created_at_ts": 0},
    )
    ok, reason = executor.can_accept_signal("XAUUSD", "LONG", "BREAKOUT_RETEST_CONTINUATION", "fp-breakout", "wait_retest")
    assert ok is False
    assert reason == "pending_order_exists"
