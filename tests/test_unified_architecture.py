from __future__ import annotations

import inspect
from unittest.mock import patch

from main import TradingBot
import lebprim_backtest_runner
from services.backtest_runner import BacktestRunner
from services.manual_trade_service import ManualTradeService
from services.simulated_executor import SimulatedExecutor
from strategy_factory import build_strategy_engine
from strategy import StrategyEngine
from trading_bot.config.schema import ConfigSchema
from trading_bot.core.execution_mode import V1_BASELINE, V2_FULL
from trading_bot.core.models import ExecutionRequest, PositionState
from trading_bot.execution.broker_base import BrokerBase
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.replay_broker import ReplayBroker
from trading_bot.core.reasons import JournalEventType, ReasonCode
from trading_bot.positions.live_lifecycle import LivePositionLifecycleManager
from trading_bot.positions.manager import PositionManager
from trading_bot.strategy.engine import UnifiedStrategyEngine


def _minimal_config() -> dict:
    return {
        "bot": {"trading_mode": "DRY_RUN"},
        "mt5": {"symbol": "XAUUSD"},
        "symbols": {},
        "dashboard": {},
        "telegram": {},
        "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
        "sessions": {"buckets": [], "live_allowed_buckets": [], "allow_asia_session": True},
        "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
        "regime": {},
        "execution": {"max_spread_points": 25.0},
        "risk": {"risk_percent": 0.25},
        "backtest": {},
        "indicators": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_trend": 200,
            "rsi_period": 14,
            "atr_period": 14,
            "adx_period": 14,
            "volume_period": 20,
        },
    }


def test_strategy_factory_returns_unified_engine() -> None:
    config = _minimal_config()
    config["bot"]["execution_mode"] = V2_FULL
    engine = build_strategy_engine(config)
    assert isinstance(engine, UnifiedStrategyEngine)
    assert config["bot"]["execution_mode_summary"]["engine_type"] == "unified_strategy_engine"
    assert "LEBPRIM_SCALP" in engine.registry.families()
    assert "TREND_PULLBACK_RECLAIM" in engine.registry.families()


def test_strategy_factory_returns_legacy_engine_for_v1() -> None:
    config = _minimal_config()
    config["bot"]["execution_mode"] = V1_BASELINE
    engine = build_strategy_engine(config)

    assert isinstance(engine, StrategyEngine)
    assert not isinstance(engine, UnifiedStrategyEngine)
    assert getattr(engine, "engine_type") == "legacy_strategy_engine"
    assert config["bot"]["execution_mode_summary"]["engine_type"] == "legacy_strategy_engine"


def test_v1_engine_does_not_call_unified_candidate_validation() -> None:
    config = _minimal_config()
    config["bot"]["execution_mode"] = V1_BASELINE
    engine = build_strategy_engine(config)

    with patch("trading_bot.strategy.validation.CandidateQualityValidator.evaluate") as evaluate:
        candidates = engine.generate_setup_candidates(
            {
                "session": {"session_name": "LONDON", "session_live_allowed": True},
                "regime": {"regime_name": "TREND_CONTINUATION", "live_allowed": True},
                "bias": {"direction": "NONE"},
            },
            None,
            None,
            None,
            {"digits": 2, "point": 0.01},
        )

    assert candidates == []
    evaluate.assert_not_called()


def test_lebprim_backtest_wrapper_is_forwarder_only() -> None:
    source = inspect.getsource(lebprim_backtest_runner.run_lebprim_backtest)
    module_source = inspect.getsource(lebprim_backtest_runner)
    assert "BacktestRunner(base, config, db)" in source
    assert "runner.run(request)" in source
    assert "LebprimStrategy(" not in module_source
    assert "monkey" not in module_source.lower()


def test_backtest_selection_enables_selected_families_for_replay() -> None:
    config = _minimal_config()
    config["strategy"]["setup_families"] = {
        "breakout_retest_continuation": {"enabled": False, "live_allowed": False},
        "compression_release": {"enabled": False, "live_allowed": False},
        "lebprim_scalp": {"enabled": True, "live_allowed": True},
    }
    config["strategy"]["setup_controls"] = {
        "breakout_retest_continuation": {"enabled": False},
        "compression_release": {"enabled": False},
        "lebprim_scalp": {"enabled": True},
    }

    BacktestRunner._apply_backtest_strategy_selection(
        config,
        ["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS"],
    )

    assert config["strategy"]["setup_families"]["breakout_retest_continuation"]["enabled"] is True
    assert config["strategy"]["setup_families"]["breakout_retest_continuation"]["live_allowed"] is True
    assert config["strategy"]["setup_controls"]["breakout_retest_continuation"]["enabled"] is True
    assert config["strategy"]["setup_families"]["compression_release"]["enabled"] is True
    assert config["strategy"]["setup_families"]["compression_release"]["live_allowed"] is True
    assert config["strategy"]["setup_controls"]["compression_release"]["enabled"] is True
    assert config["strategy"]["setup_families"]["lebprim_scalp"]["enabled"] is False
    assert config["strategy"]["setup_families"]["lebprim_scalp"]["live_allowed"] is False
    assert config["strategy"]["setup_controls"]["lebprim_scalp"]["enabled"] is False


def test_live_startup_uses_strategy_factory() -> None:
    source = inspect.getsource(TradingBot.__init__)
    assert "build_strategy_engine(self.config, self.base_dir)" in source


def test_backtest_runner_rebuilds_mode_aware_engine_for_run_config() -> None:
    source = inspect.getsource(BacktestRunner.run)
    assert "self.strategy = build_strategy_engine(run_config, self.base_dir)" in source


def test_live_runtime_uses_execution_service_for_orders() -> None:
    init_source = inspect.getsource(TradingBot.__init__)
    cycle_source = inspect.getsource(TradingBot)
    assert "ExecutionService(MT5Broker(self.connector))" in init_source
    assert "self.execution_service.submit_order" in cycle_source
    assert 'order_type="limit" if uses_limit_entry else "market"' in cycle_source
    assert "self.live_lifecycle.sync_pending_order" in cycle_source
    assert "self.live_lifecycle.apply_management_actions" in cycle_source
    assert "self.connector.send_market_order(" not in cycle_source
    assert "self.connector.close_partial(" not in cycle_source
    assert "self.connector.modify_position_sltp(" not in cycle_source


def test_manual_trade_service_uses_execution_service_boundary() -> None:
    source = inspect.getsource(ManualTradeService)
    assert "ExecutionService(MT5Broker(connector))" in source
    assert "connector.send_market_order(" not in source
    assert "connector.close_partial(" not in source
    assert "connector.close_position(" not in source
    assert "connector.modify_position_sltp(" not in source


def test_broker_contract_exposes_position_and_account_methods() -> None:
    assert hasattr(ExecutionService, "submit_order")
    assert hasattr(ExecutionService, "modify_position")
    assert hasattr(BrokerBase, "submit_order")
    assert hasattr(BrokerBase, "cancel_order")
    assert hasattr(BrokerBase, "modify_position")
    assert hasattr(BrokerBase, "partial_close")
    assert hasattr(BrokerBase, "close_position")
    assert hasattr(BrokerBase, "get_open_positions")
    assert hasattr(BrokerBase, "get_pending_orders")
    assert hasattr(BrokerBase, "get_account_state")
    assert hasattr(BrokerBase, "poll_events")


def test_reason_vocabulary_is_shared_for_live_and_replay() -> None:
    assert ReasonCode.ORDER_PENDING.value == "pending_order_created"
    assert ReasonCode.ORDER_FILLED.value == "pending_order_filled"
    assert ReasonCode.ORDER_EXPIRED.value == "pending_order_expired"
    assert ReasonCode.PARTIAL_CLOSE.value == "partial_close"
    assert ReasonCode.MAX_DURATION_EXIT.value == "MAX_DURATION_EXIT"
    assert JournalEventType.TRADE_MANAGED.value == "TRADE_MANAGED"


def test_live_lifecycle_manager_owns_management_application() -> None:
    source = inspect.getsource(LivePositionLifecycleManager)
    assert "def apply_management_actions" in source
    assert "def sync_pending_order" in source
    assert "self.execution_service.partial_close" in source
    assert "self.execution_service.modify_position" in source
    assert "self.execution_service.close_position" in source


def test_config_schema_normalizes_shared_sections() -> None:
    config = ConfigSchema.normalize(_minimal_config())
    result = ConfigSchema.validate(config)
    assert result.valid
    assert config["runtime"]["mode"] == "DRY_RUN"
    assert config["broker"]["mt5"] is config["mt5"]
    assert config["integrations"]["telegram"] is config["telegram"]


def test_replay_broker_supports_pending_limit_contract() -> None:
    executor = SimulatedExecutor(spread_points=0, slippage_points=0, point=0.01, contract_size=100)
    broker = ReplayBroker(executor)
    result = broker.submit_order(
        ExecutionRequest(
            strategy_name="XAU_LEBPRIM",
            symbol="XAUUSD",
            side="LONG",
            order_type="limit",
            entry_price=100.0,
            volume=0.1,
            stop_loss=99.0,
            take_profit=102.0,
            metadata={
                "open_time": "2026-01-01T00:00:00+00:00",
                "setup_family": "LEBPRIM_SCALP",
                "setup_fingerprint": "fp",
                "pending_expiry_bars": 3,
                "pending_expiry_minutes": 5,
            },
        )
    )
    assert result["ok"] is True
    assert result["status"] == "pending"
    assert len(executor.pending_orders) == 1


def test_position_manager_uses_single_management_contract() -> None:
    class FakeRisk:
        def evaluate_management_actions(self, position, trigger_df, setup_df, current_price, now_utc, symbol_spec):
            assert position["direction"] == "LONG"
            return [{"action": "close_full", "reason_code": "test_exit"}]

    manager = PositionManager(FakeRisk())
    actions = manager.evaluate(
        PositionState(
            position_id="p1",
            strategy_name="XAU_LEBPRIM",
            symbol="XAUUSD",
            side="LONG",
            volume=0.1,
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            opened_at="2026-01-01T00:00:00+00:00",
            lifecycle_state="open",
        ),
        trigger_df=None,
        setup_df=None,
        current_price=100.5,
        now_utc=None,
        symbol_spec={},
    )
    assert actions == [{"action": "close_full", "reason_code": "test_exit"}]
