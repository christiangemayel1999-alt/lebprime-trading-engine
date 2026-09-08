from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from risk_manager import RiskManager
from services.config_manager import ConfigManager
from trading_bot.config.schema import ConfigSchema
from trading_bot.risk.engine import RiskEngine
from trading_bot.strategy.validation import CandidateQualityValidator


def _config() -> dict:
    return ConfigSchema.normalize(deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8"))))


def _symbol_spec() -> dict[str, float]:
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


def _candidate(*, side: str = "LONG") -> dict[str, object]:
    return {
        "setup_family": "LEBPRIM_SCALP" if side == "SHORT" else "BREAKOUT_RETEST_CONTINUATION",
        "side": side,
        "setup_score": 74.0,
        "trend_score": 72.0,
        "regime_name": "TREND_CONTINUATION",
        "regime_confidence": 84.0,
        "session_name": "LONDON",
        "session_quality_score": 90.0,
        "family_enabled": True,
        "family_live_allowed": True,
        "session_allowed": True,
        "regime_allowed": True,
        "value_price": 100.0,
        "entry_price": 100.0,
        "setup_price": 100.0,
        "intended_entry_price": 100.0,
        "structure_level": 99.2 if side == "LONG" else 100.8,
        "atr_at_setup": 1.0,
        "bias_direction": "LONG" if side == "LONG" else "SHORT",
        "bias_long_score": 78.0 if side == "LONG" else 30.0,
        "bias_short_score": 26.0 if side == "LONG" else 78.0,
        "bias_delta": 52.0 if side == "LONG" else 48.0,
        "ema_slope_atr": 0.18 if side == "LONG" else -0.18,
        "profit_headroom_atr": 1.4,
        "price_extension_atr": 0.55,
        "structure_quality_score": 72.0,
        "setup_fingerprint": f"fp-{side.lower()}",
        "anchor_time": "2026-01-01T00:00:00+00:00",
        "trigger_type": "TEST",
    }


def _trigger_bar(*, close: float = 100.05, rng: float = 0.7, body: float = 0.45, upper_wick: float = 0.12, lower_wick: float = 0.13) -> dict[str, float]:
    return {
        "close": close,
        "range": rng,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "close_location": 0.72,
    }


def test_stop_width_validation_rejects_wide_stops() -> None:
    validator = CandidateQualityValidator(_config())
    candidate = _candidate()
    candidate["structure_level"] = 91.0

    report = validator.evaluate(candidate=candidate, market_context={}, trigger_bar=_trigger_bar(), symbol_spec=_symbol_spec())

    assert report.valid is False
    assert "stop_too_wide_points" in report.blocked_reasons


def test_short_validation_rejects_weak_bearish_bias() -> None:
    validator = CandidateQualityValidator(_config())
    candidate = _candidate(side="SHORT")
    candidate["bias_direction"] = "NONE"
    candidate["bias_short_score"] = 54.0
    candidate["bias_delta"] = 6.0

    report = validator.evaluate(candidate=candidate, market_context={}, trigger_bar=_trigger_bar(close=99.9), symbol_spec=_symbol_spec())

    assert report.valid is False
    assert report.blocked_reasons[0] == "weak_short_bias"


def test_trigger_quality_rejects_noisy_candles() -> None:
    validator = CandidateQualityValidator(_config())

    report = validator.evaluate(
        candidate=_candidate(),
        market_context={},
        trigger_bar=_trigger_bar(rng=1.8, body=1.1, upper_wick=0.3, lower_wick=0.4),
        symbol_spec=_symbol_spec(),
    )

    assert report.valid is False
    assert "oversized_trigger_candle" in report.blocked_reasons


def test_entry_distance_rejects_chasing() -> None:
    validator = CandidateQualityValidator(_config())

    report = validator.evaluate(
        candidate=_candidate(),
        market_context={},
        trigger_bar=_trigger_bar(close=101.15),
        symbol_spec=_symbol_spec(),
    )

    assert report.valid is False
    assert "chasing_entry" in report.blocked_reasons


def test_risk_engine_resizes_before_rejecting() -> None:
    config = _config()
    config["risk"]["risk_percent"] = 0.003
    config["risk"]["short_risk_multiplier"] = 0.5
    config["risk"]["quality_buckets"]["b_risk_multiplier"] = 0.2
    candidate = _candidate(side="SHORT")
    candidate["setup_score"] = 60.0
    entry = {
        "entry_price": 100.0,
        "pending_entry_price": 100.0,
        "entry_mode": "aggressive_limit",
        "entry_score": 60.0,
    }
    engine = RiskEngine(config)
    decision = engine.approve_order(
        candidate=candidate,
        entry=entry,
        market_context={"regime": {"regime_name": "TREND_CONTINUATION"}},
        account_info=SimpleNamespace(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0),
        symbol_spec=_symbol_spec(),
        spread_points=10.0,
        live_requested=True,
        reduced_risk=False,
    )

    assert decision.approved is True
    assert decision.reason_code == "risk_approved_resized"
    assert float(decision.volume) == 0.01
    assert decision.metadata["sizing"]["reason_code"] == "risk_resized_to_min_volume"


def test_momentum_failure_exit_needs_more_than_early_entry_revisit() -> None:
    manager = RiskManager(_config())
    position = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl": 99.0,
        "tp1": 101.0,
        "tp2": 102.0,
        "volume": 0.5,
        "opened_at": "2026-04-20T10:00:00+00:00",
        "initial_risk_price": 1.0,
        "setup_family": "LEBPRIM_SCALP",
        "entry_score": 68.0,
        "management_profile_key": "lebprim",
        "management_bars_seen": 1,
        "partial_closed": False,
        "breakeven_moved": False,
        "mfe": 0.08,
        "mae": 0.15,
    }
    trigger_df = pd.DataFrame(
        [
            {"close": 99.96, "high": 100.18, "low": 99.74, "ema_20": 99.90},
            {"close": 100.02, "high": 100.14, "low": 99.85, "ema_20": 99.92},
            {"close": 100.00, "high": 100.10, "low": 99.88, "ema_20": 99.95},
        ]
    )
    setup_df = pd.DataFrame([{"atr": 1.0, "high": 100.1, "low": 99.8}] * 5)

    actions = manager.evaluate_management_actions(
        position,
        trigger_df,
        setup_df,
        current_price=100.0,
        now_utc=pd.Timestamp("2026-04-20T10:03:00+00:00").to_pydatetime(),
        symbol_spec=_symbol_spec(),
    )

    assert "momentum_failure_exit" not in {action.get("reason_code") for action in actions}


def test_staged_time_stop_closes_stalled_trade() -> None:
    manager = RiskManager(_config())
    position = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl": 99.0,
        "tp1": 101.0,
        "tp2": 102.0,
        "volume": 0.5,
        "opened_at": "2026-04-20T09:30:00+00:00",
        "initial_risk_price": 1.0,
        "setup_family": "BREAKOUT_RETEST_CONTINUATION",
        "entry_score": 66.0,
        "management_profile_key": "breakout",
        "management_bars_seen": 6,
        "partial_closed": False,
        "breakeven_moved": False,
        "mfe": 0.20,
        "mae": 0.70,
    }
    trigger_df = pd.DataFrame(
        [
            {"close": 99.55, "high": 99.92, "low": 99.28, "ema_20": 99.90},
            {"close": 99.48, "high": 99.70, "low": 99.20, "ema_20": 99.82},
            {"close": 99.42, "high": 99.60, "low": 99.18, "ema_20": 99.75},
            {"close": 99.36, "high": 99.54, "low": 99.12, "ema_20": 99.68},
        ]
    )
    setup_df = pd.DataFrame([{"atr": 1.0, "high": 99.8, "low": 99.1}] * 5)

    actions = manager.evaluate_management_actions(
        position,
        trigger_df,
        setup_df,
        current_price=99.36,
        now_utc=pd.Timestamp("2026-04-20T10:00:00+00:00").to_pydatetime(),
        symbol_spec=_symbol_spec(),
    )

    assert "time_stop_exit" in {action.get("reason_code") for action in actions}


def test_resolved_runtime_snapshot_exposes_authoritative_policy(tmp_path: Path) -> None:
    base_dir = tmp_path
    if not os.access(base_dir, os.W_OK):
        base_dir = Path("storage") / "tmp_v21_runtime_snapshot"
    config = deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))
    config["storage"] = {"database_path": "storage/test.db", "state_path": "storage/state.json"}
    config["strategy"]["setup_controls"]["compression_release"]["enabled"] = False
    config["strategy"]["setup_families"]["lebprim_scalp"]["enabled"] = False
    (base_dir / "storage").mkdir(parents=True, exist_ok=True)
    (base_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    manager = ConfigManager(base_dir)
    snapshot = manager.resolved_runtime_snapshot()

    assert snapshot["mode"] == str(config["bot"]["trading_mode"])
    assert "enabled_strategies" in snapshot
    assert "disabled_strategies" in snapshot
    assert "active_execution_policy" in snapshot
    assert snapshot["skipped_entities"]["lebprim_scalp"] == "disabled_in_setup_families"
