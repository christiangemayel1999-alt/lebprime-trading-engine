from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from trading_bot.backtest.oos_evaluation import OOSEvaluationFramework, ScenarioResult


def _config() -> dict:
    return deepcopy(json.loads(Path("config.json").read_text(encoding="utf-8")))


def _signal(
    ts: str,
    *,
    strategy_name: str,
    setup_family: str,
    side: str,
    entry_score: float,
    execution_action: str,
    executed: bool,
    blocked_reason: str = "",
    signal_status: str = "",
    value_distance_atr: float = 0.4,
) -> dict:
    return {
        "signal_time": ts,
        "strategy_name": strategy_name,
        "symbol": "XAUUSD",
        "side": side,
        "setup": setup_family,
        "entry": 100.0,
        "sl": None,
        "tp": None,
        "score": entry_score,
        "executed": executed,
        "execution_reason": "executed" if executed else ("pending_order_created" if execution_action in {"WAIT_RETEST", "PLACE_LIMIT"} and signal_status == "pending" else None),
        "blocked_reason": blocked_reason or None,
        "raw_signal_json": {
            "candidate": {
                "setup_family": setup_family,
                "setup_fingerprint": f"{setup_family}|{side}|{ts}",
                "setup_valid": True,
                "side": side,
                "regime_name": "TREND_CONTINUATION",
                "regime_confidence": 85.0,
                "session_name": "LONDON",
                "session_quality_score": 88.0,
                "value_price": 100.0,
            },
            "entry": {
                "entry_mode": "aggressive" if execution_action == "EXECUTE_NOW" else "wait_retest" if execution_action == "WAIT_RETEST" else "limit_value",
                "entry_price": 100.0,
                "pending_entry_price": 99.8 if execution_action in {"WAIT_RETEST", "PLACE_LIMIT"} else None,
                "entry_score": entry_score,
                "trigger_score": entry_score - 5.0,
                "setup_score": entry_score - 8.0,
                "trend_score": entry_score + 3.0,
                "value_distance_atr": value_distance_atr,
                "trigger_candle_atr": 0.9,
                "valid": execution_action != "BLOCK",
                "live_ready": execution_action != "BLOCK",
            },
            "execution_decision": {
                "action": execution_action,
                "reason_code": blocked_reason or ("execute_now" if execution_action == "EXECUTE_NOW" else "deferred_wait_retest"),
                "reason": blocked_reason or execution_action.lower(),
                "entry_price": 99.8 if execution_action in {"WAIT_RETEST", "PLACE_LIMIT"} else 100.0,
                "reference_price": 100.0,
                "pending_expiry_minutes": 5,
                "pending_expiry_bars": 3,
                "metadata": {
                    "strategy_family": "BREAKOUT" if "BREAKOUT" in setup_family else "COMPRESS",
                    "management_profile": "BreakoutManagementProfile" if "BREAKOUT" in setup_family else "CompressManagementProfile",
                    "quality_tier": "high",
                    "volatility_state": "normal",
                },
            },
            "diagnostics": {
                "setup_valid": True,
                "signal_status": signal_status or ("filled" if executed else "rejected"),
                "entry_score": entry_score,
                "trigger_score": entry_score - 5.0,
                "entry_mode": "aggressive" if execution_action == "EXECUTE_NOW" else "wait_retest" if execution_action == "WAIT_RETEST" else "limit_value",
                "blocked_reason": blocked_reason or "",
                "quality_tier": "high",
                "management_profile": "BreakoutManagementProfile" if "BREAKOUT" in setup_family else "CompressManagementProfile",
            },
        },
    }


def _trade(
    open_time: str,
    close_time: str,
    *,
    strategy_name: str,
    setup_family: str,
    side: str,
    pnl: float,
    execution_action: str,
    tp1_hit: bool = False,
    breakeven: bool = False,
    momentum_failure: bool = False,
) -> dict:
    management_events: list[dict[str, object]] = []
    if tp1_hit:
        management_events.append({"ts": close_time, "action": "partial_close", "reason_code": "partial_tp1", "price": 101.0, "volume": 0.5, "pnl": max(pnl, 0.0) / 2.0})
    if breakeven:
        management_events.append({"ts": close_time, "action": "move_stop", "reason_code": "breakeven_after_tp1", "sl": 100.0})
    if momentum_failure:
        management_events.append({"ts": close_time, "action": "close_full", "reason_code": "momentum_failure_exit", "price": 99.4})
    return {
        "strategy_name": strategy_name,
        "symbol": "XAUUSD",
        "side": side,
        "open_time": open_time,
        "close_time": close_time,
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "exit_price": 101.4 if pnl > 0 else 99.4,
        "pnl": pnl,
        "pnl_r": pnl / 100.0,
        "exit_reason": "momentum_failure_exit" if momentum_failure else "manual",
        "exit_reason_code": "momentum_failure_exit" if momentum_failure else "manual_exit",
        "mfe": 2.0,
        "mae": 0.6,
        "duration_seconds": 300.0,
        "trade_metadata_json": {
            "setup_family": setup_family,
            "strategy_family": "BREAKOUT" if "BREAKOUT" in setup_family else "COMPRESS",
            "entry_mode": "aggressive" if execution_action == "EXECUTE_NOW" else "wait_retest",
            "execution_decision": execution_action,
            "quality_tier": "high",
            "management_profile": "BreakoutManagementProfile" if "BREAKOUT" in setup_family else "CompressManagementProfile",
            "volatility_state": "normal",
            "pending_entry_price": 99.8 if execution_action == "WAIT_RETEST" else None,
            "tp1": 101.0,
            "setup_fingerprint": f"{setup_family}|{side}|{open_time}",
            "entry": {"entry_score": 72.0, "value_distance_atr": 0.35},
            "candidate": {
                "setup_family": setup_family,
                "side": side,
                "regime_name": "TREND_CONTINUATION",
                "session_name": "LONDON",
            },
            "management_events": management_events,
        },
    }


def _bundle(start_date: str, end_date: str, signals: list[dict], trades: list[dict]) -> dict:
    return {
        "run": {
            "id": 1,
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": start_date,
            "end_date": end_date,
        },
        "signals": signals,
        "trades": trades,
        "equity_curve": [
            {"ts": start_date, "equity": 10000.0, "balance": 10000.0, "floating_pnl": 0.0},
            {"ts": end_date, "equity": 10180.0 if trades and sum(float(t["pnl"]) for t in trades) > 0 else 10020.0, "balance": 10180.0 if trades and sum(float(t["pnl"]) for t in trades) > 0 else 10020.0, "floating_pnl": 0.0},
        ],
    }


def test_default_oos_scenarios_cover_required_matrix() -> None:
    scenarios = OOSEvaluationFramework.default_scenarios()
    keys = [scenario.key for scenario in scenarios]
    assert keys == [
        "bot_v1_baseline",
        "bot_v2_full",
        "bot_v2_lebprim_disabled",
        "bot_v2_multi_position_disabled",
        "bot_v2_adaptive_only",
        "bot_v2_family_management_only",
    ]
    full = next(item for item in scenarios if item.key == "bot_v2_full")
    assert full.overrides["execution"]["allow_multi_position"] is True
    assert full.overrides["exit"]["enable_family_specific_management"] is True


def test_oos_report_detects_quality_and_participation_improvement(tmp_path: Path) -> None:
    framework = OOSEvaluationFramework(tmp_path, _config(), database_path=tmp_path / "oos.db")
    baseline = ScenarioResult(
        key="bot_v1_baseline",
        label="Bot V1 Baseline",
        source="external_bundle",
        description="baseline",
        bundle=_bundle(
            "2026-04-15T00:00:00+00:00",
            "2026-04-16T23:59:59+00:00",
            [
                _signal("2026-04-15T09:00:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=65.0, execution_action="EXECUTE_NOW", executed=True),
                _signal("2026-04-15T10:00:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=61.0, execution_action="EXECUTE_NOW", executed=True),
                _signal("2026-04-15T11:00:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=54.0, execution_action="BLOCK", executed=False, blocked_reason="blocked_chasing_entry"),
                _signal("2026-04-15T12:00:00+00:00", strategy_name="XAU_BOT_COMPRESS", setup_family="COMPRESSION_RELEASE", side="SHORT", entry_score=53.0, execution_action="BLOCK", executed=False, blocked_reason="position_exists"),
            ],
            [
                _trade("2026-04-15T09:00:00+00:00", "2026-04-15T09:20:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", pnl=100.0, execution_action="EXECUTE_NOW", tp1_hit=True, breakeven=True),
                _trade("2026-04-15T10:00:00+00:00", "2026-04-15T10:15:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", pnl=-80.0, execution_action="EXECUTE_NOW", momentum_failure=True),
            ],
        ),
    )
    v2_full = ScenarioResult(
        key="bot_v2_full",
        label="Bot V2 Full",
        source="run",
        description="v2",
        bundle=_bundle(
            "2026-04-15T00:00:00+00:00",
            "2026-04-16T23:59:59+00:00",
            [
                _signal("2026-04-15T09:00:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=67.0, execution_action="EXECUTE_NOW", executed=True),
                _signal("2026-04-15T09:30:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=66.0, execution_action="WAIT_RETEST", executed=False, signal_status="pending"),
                _signal("2026-04-15T10:00:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", entry_score=63.0, execution_action="EXECUTE_NOW", executed=True),
                _signal("2026-04-15T11:00:00+00:00", strategy_name="XAU_BOT_COMPRESS", setup_family="COMPRESSION_RELEASE", side="SHORT", entry_score=62.0, execution_action="EXECUTE_NOW", executed=True),
                _signal("2026-04-15T12:00:00+00:00", strategy_name="XAU_BOT_COMPRESS", setup_family="COMPRESSION_RELEASE", side="SHORT", entry_score=55.0, execution_action="BLOCK", executed=False, blocked_reason="blocked_bad_session"),
            ],
            [
                _trade("2026-04-15T09:00:00+00:00", "2026-04-15T09:20:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", pnl=120.0, execution_action="EXECUTE_NOW", tp1_hit=True, breakeven=True),
                _trade("2026-04-15T09:30:00+00:00", "2026-04-15T09:50:00+00:00", strategy_name="XAU_BOT_BREAKOUT", setup_family="BREAKOUT_RETEST_CONTINUATION", side="LONG", pnl=60.0, execution_action="WAIT_RETEST", tp1_hit=True),
                _trade("2026-04-15T11:00:00+00:00", "2026-04-15T11:10:00+00:00", strategy_name="XAU_BOT_COMPRESS", setup_family="COMPRESSION_RELEASE", side="SHORT", pnl=40.0, execution_action="EXECUTE_NOW"),
            ],
        ),
    )
    report = framework._build_report(
        {"start_date": "2026-04-15", "end_date": "2026-04-16"},
        [baseline, v2_full],
    )
    comparison = report["tables"]["scenario_comparison"]
    verdicts = report["quality_verdicts"]
    assert not comparison.empty
    row = comparison.loc[comparison["scenario_key"] == "bot_v2_full"].iloc[0]
    assert float(row["delta_net_profit"]) > 0.0
    assert float(row["delta_executed_rate_pct"]) > 0.0
    assert not report["tables"]["deferred_conversion"].empty
    verdict = next(item for item in verdicts if item["scenario_key"] == "bot_v2_full")
    assert verdict["verdict"] in {"improved_quality_and_participation", "quality_led_improvement"}
