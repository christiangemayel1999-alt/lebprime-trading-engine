"""Replay-session components for deterministic backtest execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

import pandas as pd

from services.execution_flow import resolve_spread_limit
from services.historical_feed import HistoricalMT5Feed
from services.simulated_executor import SimulatedExecutor
from trading_bot.core.job_state import JobState
from trading_bot.core.reasons import ManagementAction, ReasonCode
from trading_bot.execution.canonical import CanonicalExecutionPlan, build_execution_request_from_plan
from trading_bot.execution.decision_engine import BLOCK
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.exposure_policy import ExposurePolicy
from trading_bot.execution.pending_policy import PendingOrderPolicy
from trading_bot.risk.engine import RiskEngine
from trading_bot.strategy.diagnostics import build_strategy_candidate_diagnostics
from utils import to_utc, utc_now


@dataclass
class SimulatedAccount:
    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 1000.0
    leverage: float = 1.0
    peak_equity: float = 0.0
    daily_start_equity: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str | None = None
    account_status: str = "ACTIVE"
    blown_at_time: str | None = None
    equity_at_stop: float | None = None
    trades_after_stop_prevented: int = 0
    rejected_insufficient_margin_count: int = 0
    last_equity_date: str | None = None

    def __post_init__(self) -> None:
        self.margin_free = float(self.margin_free or self.balance)
        self.peak_equity = float(self.peak_equity or self.equity or self.balance)
        self.daily_start_equity = float(self.daily_start_equity or self.equity or self.balance)

    def required_margin(self, *, price: float, volume: float, contract_size: float) -> float:
        leverage = max(float(self.leverage or 1.0), 1e-9)
        notional = abs(float(price) * float(volume) * float(contract_size))
        return notional / leverage

    def refresh(self, *, executor: SimulatedExecutor, mark_price: float, now_utc: datetime, contract_size: float) -> None:
        if self.last_equity_date != now_utc.date().isoformat():
            self.daily_start_equity = float(self.balance)
            self.last_equity_date = now_utc.date().isoformat()
        self.margin = sum(
            self.required_margin(
                price=float(getattr(trade, "entry", mark_price) or mark_price),
                volume=float(getattr(trade, "volume", 0.0) or 0.0),
                contract_size=contract_size,
            )
            for trade in executor.open_trades
            if not getattr(trade, "closed", False)
        )
        floating = float(executor.floating_pnl(mark_price))
        self.equity = float(self.balance) + floating
        self.margin_free = float(self.equity) - float(self.margin)
        self.margin_level = (float(self.equity) / float(self.margin) * 100.0) if self.margin > 0 else 0.0
        self.peak_equity = max(float(self.peak_equity), float(self.equity))


_SimAccount = SimulatedAccount


@dataclass(frozen=True)
class ReplayWindow:
    trend: pd.DataFrame
    setup: pd.DataFrame
    trigger: pd.DataFrame

    @property
    def has_minimum_warmup(self) -> bool:
        return len(self.trend) >= 50 and len(self.setup) >= 50 and len(self.trigger) >= 50


class ReplayMarketData:
    """Provides only the candle windows visible at the current replay timestamp."""

    def __init__(self, *, trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, setup_tf: str, trend_tf: str) -> None:
        self._trend_df = trend_df
        self._setup_df = setup_df
        self._trigger_df = trigger_df
        self._setup_bar_duration = pd.Timedelta(setup_tf)
        self._trend_bar_duration = pd.Timedelta(trend_tf)

    def window_at(self, current_timestamp: datetime) -> ReplayWindow:
        trend_slice = self._trend_df[self._trend_df["time"] + self._trend_bar_duration <= current_timestamp].copy()
        setup_slice = self._setup_df[self._setup_df["time"] + self._setup_bar_duration <= current_timestamp].copy()
        trigger_slice = self._trigger_df[self._trigger_df["time"] <= current_timestamp].copy()
        return ReplayWindow(trend=trend_slice, setup=setup_slice, trigger=trigger_slice)


class ReplayClock:
    """Owns replay progression and exposes only the current/next bar cursor."""

    def __init__(self, feed: HistoricalMT5Feed, total_bars: int) -> None:
        self.feed = feed
        self.progress_total = max(int(total_bars), 1)
        self.bar_index = -1

    def next_bar(self) -> dict[str, Any] | None:
        bar = self.feed.get_next_bar()
        if bar is None:
            return None
        self.bar_index += 1
        return bar

    def peek_next_bar(self) -> dict[str, Any] | None:
        return self.feed.peek_next_bar()

    def get_current_bar(self) -> dict[str, Any] | None:
        return self.feed.get_current_bar()


class ReplayRecorder:
    """Owns replay persistence calls while preserving the existing storage schema."""

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    def store_equity(self, payload: dict[str, Any]) -> None:
        self._runner.storage.store_equity(payload)

    def store_signal(self, payload: dict[str, Any]) -> None:
        self._runner.storage.store_signal(payload)

    def frame(
        self,
        *,
        run_id: int,
        bar_index: int,
        bar_ts_iso: str,
        bar: dict[str, Any],
        balance: float,
        executor: SimulatedExecutor,
        candidate: dict[str, Any] | None,
        candidate_summary: dict[str, Any],
    ) -> None:
        self._runner._record_playback_frame(
            run_id=run_id,
            bar_index=bar_index,
            bar_ts_iso=bar_ts_iso,
            bar=bar,
            balance=balance,
            executor=executor,
            candidate=candidate,
            candidate_summary=candidate_summary,
        )

    def event(
        self,
        *,
        run_id: int,
        bar_index: int,
        timestamp: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self._runner._record_playback_event(
            run_id=run_id,
            bar_index=bar_index,
            timestamp=timestamp,
            event_type=event_type,
            payload=payload,
        )

    def pending_resolution_signal(
        self,
        *,
        run_id: int,
        ts_iso: str,
        event: dict[str, Any],
        blocked_reason: str,
        blocker_source: str,
    ) -> None:
        self._runner._store_pending_resolution_signal(
            run_id=run_id,
            ts_iso=ts_iso,
            event=event,
            blocked_reason=blocked_reason,
            blocker_source=blocker_source,
        )

    def persist_trade_close(
        self,
        *,
        run_id: int,
        bar_index: int,
        timestamp: str,
        trade_row: dict[str, Any],
        balance_before: float,
        equity_before: float,
        source: str,
        event_type: str = "trade_closed",
        extra_event_payload: dict[str, Any] | None = None,
    ) -> float:
        return self._runner._persist_trade_close(
            run_id=run_id,
            bar_index=bar_index,
            timestamp=timestamp,
            trade_row=trade_row,
            balance_before=balance_before,
            equity_before=equity_before,
            source=source,
            event_type=event_type,
            extra_event_payload=extra_event_payload,
        )


@dataclass(frozen=True)
class StrategyEvaluationResult:
    market: dict[str, Any]
    raw_candidates: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    selected_candidate: dict[str, Any] | None


class StrategyReplayEvaluator:
    """Runs replay strategy evaluation through the configured strategy engine."""

    def __init__(self, *, strategy: Any, logger: Any, config: dict[str, Any], enabled_families: set[str]) -> None:
        self._strategy = strategy
        self._logger = logger
        self._config = config
        self._enabled_families = enabled_families

    def analyze_market(
        self,
        *,
        bar_ts: datetime,
        trend_slice: pd.DataFrame,
        setup_slice: pd.DataFrame,
        trigger_slice: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]:
        return self._strategy.analyze_market_context(bar_ts, trend_slice, setup_slice, trigger_slice, spread_points)

    def evaluate_setups(
        self,
        *,
        market: dict[str, Any],
        trend_slice: pd.DataFrame,
        setup_slice: pd.DataFrame,
        trigger_slice: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> StrategyEvaluationResult:
        raw_candidates = self._strategy.generate_setup_candidates(market, trend_slice, setup_slice, trigger_slice, symbol_spec)
        candidates = [item for item in raw_candidates if str(item.get("setup_family") or "") in self._enabled_families]
        selected_candidate = self._strategy.choose_best_setup(candidates)
        self._logger.structured(
            "strategy_candidate_diagnostics",
            build_strategy_candidate_diagnostics(
                config=self._config,
                engine=self._strategy,
                market_context=market,
                candidates=raw_candidates,
                selected_candidate=selected_candidate,
                runtime_mode="BACKTEST",
                allowed_families=self._enabled_families,
            ),
        )
        return StrategyEvaluationResult(
            market=market,
            raw_candidates=raw_candidates,
            candidates=candidates,
            selected_candidate=selected_candidate,
        )

    def evaluate_entry(
        self,
        candidate: dict[str, Any],
        trigger_slice: pd.DataFrame,
        symbol_spec: dict[str, Any],
        bar_ts: datetime,
    ) -> dict[str, Any]:
        return self._strategy.evaluate_entry(candidate, trigger_slice, symbol_spec, bar_ts, live_profile=True)


@dataclass
class ReplayContext:
    request: dict[str, Any]
    run_config: dict[str, Any]
    run_id: int
    symbol: str
    timeframe: str
    start_dt: datetime
    end_dt: datetime
    requested_engine_mode: str
    resolved_mode_summary: dict[str, Any]
    selected_strategies: list[str]
    enabled_families: set[str]
    history_resolution: Any
    effective_first_bar: datetime | None
    effective_last_bar: datetime | None
    effective_replay_first: datetime | None
    effective_replay_last: datetime | None
    clock: ReplayClock
    market_data: ReplayMarketData
    symbol_spec: dict[str, Any]
    spread_model: dict[str, Any]
    slippage_model: dict[str, Any]
    execution_model: str
    execution_router: Any
    exposure_policy: ExposurePolicy
    pending_policy: PendingOrderPolicy
    executor: SimulatedExecutor
    execution_service: ExecutionService
    risk_manager: Any
    risk_engine: RiskEngine
    protection_cfg: dict[str, Any]
    weekend_cfg: dict[str, Any]
    fingerprint_guard_cfg: dict[str, Any]
    friday_hard_close: time
    friday_entry_cutoff: time
    session_filter_enabled: bool
    allowed_sessions: set[str]


class ReplaySession:
    """Coordinates one historical replay while preserving existing runner helpers."""

    def __init__(self, runner: Any, context: ReplayContext) -> None:
        self._runner = runner
        self.context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runner, name)

    def execute(self, *, progress_callback: Any | None = None, should_interrupt: Any | None = None) -> dict[str, Any]:
        request = self.context.request
        run_config = self.context.run_config
        run_id = self.context.run_id
        symbol = self.context.symbol
        timeframe = self.context.timeframe
        start_dt = self.context.start_dt
        end_dt = self.context.end_dt
        requested_engine_mode = self.context.requested_engine_mode
        resolved_mode_summary = self.context.resolved_mode_summary
        selected_strategies = self.context.selected_strategies
        enabled_families = self.context.enabled_families
        history_resolution = self.context.history_resolution
        effective_first_bar = self.context.effective_first_bar
        effective_last_bar = self.context.effective_last_bar
        effective_replay_first = self.context.effective_replay_first
        effective_replay_last = self.context.effective_replay_last
        symbol_spec = self.context.symbol_spec
        spread_model = self.context.spread_model
        slippage_model = self.context.slippage_model
        execution_model = self.context.execution_model
        execution_router = self.context.execution_router
        exposure_policy = self.context.exposure_policy
        pending_policy = self.context.pending_policy
        executor = self.context.executor
        execution_service = self.context.execution_service
        risk_manager = self.context.risk_manager
        risk_engine = self.context.risk_engine
        protection_cfg = self.context.protection_cfg
        weekend_cfg = self.context.weekend_cfg
        fingerprint_guard_cfg = self.context.fingerprint_guard_cfg
        friday_hard_close = self.context.friday_hard_close
        friday_entry_cutoff = self.context.friday_entry_cutoff
        session_filter_enabled = self.context.session_filter_enabled
        allowed_sessions = self.context.allowed_sessions
        recorder = ReplayRecorder(self._runner)
        strategy_evaluator = StrategyReplayEvaluator(
            strategy=self.strategy,
            logger=self.logger,
            config=run_config,
            enabled_families=enabled_families,
        )
        balance = float(request.get("initial_balance", 10000.0))
        leverage = float(request.get("leverage", self.config.get("backtest", {}).get("leverage", 100.0)) or 100.0)
        protection_cfg = dict(run_config.get("risk", {}).get("backtest_account_protection", {}) or {})
        weekend_cfg = dict(run_config.get("risk", {}).get("weekend_protection", {}) or {})
        fingerprint_guard_cfg = dict(run_config.get("execution", {}).get("setup_fingerprint_guard", {}) or {})
        friday_hard_close = self._clock_time_utc(weekend_cfg.get("friday_hard_close_time_utc", "21:00"), "21:00")
        friday_entry_cutoff = self._clock_time_utc(weekend_cfg.get("block_new_entries_after_utc", "20:30"), "20:30")
        account = _SimAccount(
            balance=balance,
            equity=balance,
            margin_free=balance,
            leverage=leverage,
            peak_equity=balance,
            daily_start_equity=balance,
        )
        blocked_fingerprints: dict[str, dict[str, Any]] = {}
        last_attempt_at: dict[str, datetime] = {}
        min_duplicate_gap = int(self.config.get("execution", {}).get("min_seconds_between_duplicate_attempts", 45))
        clock = self.context.clock
        market_data = self.context.market_data
        bar_index = clock.bar_index
        progress_total = clock.progress_total
        self.database.update_backtest_run(
            run_id,
            {
                "progress_current": 0,
                "progress_total": progress_total,
                "last_heartbeat_at": utc_now().isoformat(),
            },
        )

        while True:
            if callable(should_interrupt) and bool(should_interrupt()):
                raise InterruptedError("backtest_job_cancel_requested")
            bar = clock.next_bar()
            if bar is None:
                break
            bar_index = clock.bar_index
            if bar_index == 0 or bar_index % 250 == 0 or bar_index + 1 >= progress_total:
                self.database.update_backtest_run(
                    run_id,
                    {
                        "state": JobState.RUNNING.value,
                        "progress_current": min(bar_index + 1, progress_total),
                        "progress_total": progress_total,
                        "last_heartbeat_at": utc_now().isoformat(),
                    },
                )
                if callable(progress_callback):
                    progress_callback(run_id, min(bar_index + 1, progress_total), progress_total)
            bar_ts = to_utc(bar["time"])
            if bar_ts is None:
                continue
            bar_ts_iso = bar_ts.isoformat()
            current_close = float(bar["close"])
            account.balance = float(balance)
            self._refresh_backtest_account(
                account,
                executor,
                mark_price=current_close,
                now_utc=bar_ts,
                contract_size=float(symbol_spec["contract_size"]),
            )
            candidate_summary: dict[str, Any] = {
                "source_kind": history_resolution.source_kind,
                "account_status": account.account_status,
            }
            stop_processing_after_bar = False
            # Write a bar snapshot immediately so every processed bar has a persisted frame
            # even if a later branch exits early. The final end-of-bar snapshot will overwrite
            # this same bar_index with the latest state.
            recorder.frame(
                run_id=run_id,
                bar_index=bar_index,
                bar_ts_iso=bar_ts_iso,
                bar=bar,
                balance=balance,
                executor=executor,
                candidate=None,
                candidate_summary={**candidate_summary, "phase": "pre_bar"},
            )

            # Look-ahead-free slicing: a higher-TF bar opened at T is only
            # visible once the current trigger bar's open time >= T + bar_duration
            # (i.e. the higher-TF bar has fully closed).  The trigger slice
            # correctly includes the current bar because it is a closed M1 bar.
            window = market_data.window_at(bar_ts)
            trend_slice = window.trend
            setup_slice = window.setup
            trigger_slice = window.trigger
            if len(trend_slice) < 50 or len(setup_slice) < 50 or len(trigger_slice) < 50:
                recorder.store_equity(
                    {
                        "run_id": run_id,
                        "ts": bar_ts_iso,
                        "equity": balance + executor.floating_pnl(float(bar["close"])),
                        "balance": balance,
                        "floating_pnl": executor.floating_pnl(float(bar["close"])),
                    }
                )
                recorder.frame(
                    run_id=run_id,
                    bar_index=bar_index,
                    bar_ts_iso=bar_ts_iso,
                    bar=bar,
                    balance=balance,
                    executor=executor,
                    candidate=None,
                    candidate_summary=candidate_summary,
                )
                continue

            spread_points = self._spread_points(spread_model, bar)
            market = strategy_evaluator.analyze_market(
                bar_ts=bar_ts,
                trend_slice=trend_slice,
                setup_slice=setup_slice,
                trigger_slice=trigger_slice,
                spread_points=spread_points,
            )
            if bool(weekend_cfg.get("enabled", True)) and self._is_friday_cutoff(bar_ts, friday_hard_close):
                if bool(weekend_cfg.get("cancel_pending_orders", True)):
                    cancelled_orders = executor.cancel_pending_orders(lambda _order: True)
                    for cancelled_order in cancelled_orders:
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="pending_cancelled",
                            payload={
                                "strategy_name": str(getattr(cancelled_order, "strategy_name", "")),
                                "setup_family": str(getattr(cancelled_order, "setup", "")),
                                "side": str(getattr(cancelled_order, "side", "")),
                                "reason_code": "friday_hard_close",
                                "position_id": str(getattr(cancelled_order, "order_id", "")),
                            },
                        )
                if bool(weekend_cfg.get("close_open_positions", True)):
                    for trade in list(executor.open_trades):
                        forced_payload = executor.close_trade_now(
                            trade=trade,
                            exit_price=self._current_exit_price(trade, current_close, executor),
                            exit_reason="friday_hard_close",
                            close_time=bar_ts_iso,
                            reason_code="friday_hard_close",
                            extra_metadata={"forced_close_reason": "friday_hard_close"},
                        )
                        forced_payload["duration_seconds"] = max(
                            (bar_ts - (to_utc(forced_payload["open_time"]) or bar_ts)).total_seconds(),
                            0.0,
                        )
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="forced_close",
                            payload={
                                "strategy_name": str(forced_payload.get("strategy_name", "")),
                                "setup_family": str(forced_payload.get("setup_family", "")),
                                "side": str(forced_payload.get("side", "")),
                                "reason_code": "friday_hard_close",
                                "price": float(forced_payload.get("exit_price") or current_close),
                                "position_id": str(forced_payload.get("position_id") or forced_payload.get("open_time") or ""),
                            },
                        )
                        balance = recorder.persist_trade_close(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            trade_row=forced_payload,
                            balance_before=float(balance),
                            equity_before=float(account.equity),
                            source="backtest",
                        )
                        account.balance = float(balance)
                        self._mark_fingerprint_closed(
                            blocked_fingerprints,
                            str(forced_payload.get("setup_fingerprint") or ""),
                            closed_at=bar_ts,
                            cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                            trade_id=str(forced_payload.get("trade_id") or forced_payload.get("position_id") or forced_payload.get("open_time") or ""),
                        )

            for pending_order in list(executor.pending_orders):
                if getattr(pending_order, "status", "") != "active":
                    continue
                pending_assessment = pending_policy.assess(
                    {
                        "order_id": getattr(pending_order, "order_id", ""),
                        "setup_family": getattr(pending_order, "setup", ""),
                        "setup": getattr(pending_order, "setup", ""),
                        "direction": getattr(pending_order, "side", ""),
                        "entry_price": getattr(pending_order, "pending_entry_price", 0.0),
                        **dict(getattr(pending_order, "metadata", {}) or {}),
                    },
                    market,
                    bar,
                )
                if pending_assessment.action == "cancel":
                    cancelled = executor.cancel_pending_orders(lambda order: getattr(order, "order_id", "") == getattr(pending_order, "order_id", ""))
                    if cancelled:
                        order = cancelled[0]
                        recorder.pending_resolution_signal(
                            run_id=run_id,
                            ts_iso=bar_ts_iso,
                            event={
                                "strategy_name": order.strategy_name,
                                "symbol": order.symbol,
                                "side": order.side,
                                "setup": order.setup,
                                "pending_entry_price": order.pending_entry_price,
                                "metadata": dict(order.metadata),
                            },
                            blocked_reason=str(pending_assessment.reason_code),
                            blocker_source="pending_order_policy",
                        )

            pending_events = executor.update_pending_orders(bar, bar_ts_iso, bar_index)
            for event in pending_events:
                if event["event_type"] == "pending_filled":
                    filled_trade = event.get("trade")
                    if filled_trade is not None:
                        self._mark_fingerprint_executed(
                            blocked_fingerprints,
                            str((getattr(filled_trade, "metadata", {}) or {}).get("setup_fingerprint") or event.get("setup_fingerprint") or ""),
                            str(getattr(filled_trade, "open_time", "")),
                        )
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="pending_filled",
                            payload={
                                "strategy_name": str(getattr(filled_trade, "strategy_name", event.get("strategy_name", ""))),
                                "setup_family": str(getattr(filled_trade, "setup", event.get("setup", ""))),
                                "side": str(getattr(filled_trade, "side", event.get("side", ""))),
                                "reason_code": ReasonCode.ORDER_FILLED.value,
                                "price": float(getattr(filled_trade, "entry", event.get("pending_entry_price", 0.0)) or 0.0),
                                "position_id": str(getattr(filled_trade, "open_time", "")),
                            },
                        )
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="trade_opened",
                            payload={
                                "strategy_name": str(getattr(filled_trade, "strategy_name", event.get("strategy_name", ""))),
                                "setup_family": str(getattr(filled_trade, "setup", event.get("setup", ""))),
                                "side": str(getattr(filled_trade, "side", event.get("side", ""))),
                                "reason_code": ReasonCode.ORDER_FILLED.value,
                                "price": float(getattr(filled_trade, "entry", event.get("pending_entry_price", 0.0)) or 0.0),
                                "position_id": str(getattr(filled_trade, "open_time", "")),
                                "execution_model": execution_model,
                            },
                        )
                        self.logger.structured(
                            "backtest_trade_opened",
                            {
                                "timestamp": bar_ts_iso,
                                "side": str(getattr(filled_trade, "side", event.get("side", ""))),
                                "entry": float(getattr(filled_trade, "entry", event.get("pending_entry_price", 0.0)) or 0.0),
                                "sl": float(getattr(filled_trade, "sl", 0.0) or 0.0),
                                "tp": float(getattr(filled_trade, "tp", 0.0) or 0.0),
                                "setup_family": str(getattr(filled_trade, "setup", event.get("setup", ""))),
                                "reason_code": ReasonCode.ORDER_FILLED.value,
                            },
                        )
                elif event["event_type"] == "pending_expired":
                    recorder.event(
                        run_id=run_id,
                        bar_index=bar_index,
                        timestamp=bar_ts_iso,
                        event_type="pending_expired",
                        payload={
                            "strategy_name": str(event.get("strategy_name", "")),
                            "setup_family": str(event.get("setup", "")),
                            "side": str(event.get("side", "")),
                            "reason_code": ReasonCode.ORDER_EXPIRED.value,
                            "price": float(event.get("pending_entry_price", 0.0) or 0.0),
                            "position_id": str(event.get("order_id", "")),
                        },
                    )
                    recorder.pending_resolution_signal(
                        run_id=run_id,
                        ts_iso=bar_ts_iso,
                        event=event,
                        blocked_reason=ReasonCode.ORDER_EXPIRED.value,
                        blocker_source="pending_order_expiry",
                    )

            for trade in list(executor.open_trades):
                if str(trade.open_time) == bar_ts_iso:
                    continue
                position = self._management_position_from_trade(trade)
                management_trigger_slice, management_setup_slice = self._management_indicator_slices(trigger_slice, setup_slice)
                management_actions = risk_manager.evaluate_management_actions(
                    position=position,
                    trigger_df=management_trigger_slice,
                    setup_df=management_setup_slice,
                    current_price=current_close,
                    now_utc=bar_ts,
                    symbol_spec=symbol_spec,
                )
                self._sync_trade_from_management_position(trade, position)
                applied_events = executor.apply_management_actions(
                    trade=trade,
                    actions=management_actions,
                    current_price=current_close,
                    ts_iso=bar_ts_iso,
                    symbol_spec=symbol_spec,
                )
                for event in applied_events:
                    if event["event_type"] == ManagementAction.PARTIAL_CLOSE.value:
                        balance_before = float(balance)
                        balance += float(event.get("pnl") or 0.0)
                        account.balance = float(balance)
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="trade_partial_close",
                            payload={
                                "strategy_name": str(getattr(trade, "strategy_name", "")),
                                "setup_family": str(getattr(trade, "setup", "")),
                                "side": str(getattr(trade, "side", "")),
                                "reason_code": str(event.get("reason_code") or ReasonCode.PARTIAL_CLOSE.value),
                                "price": float(getattr(trade, "tp1", current_close) or current_close),
                                "position_id": str(getattr(trade, "open_time", "")),
                                "volume": float(event.get("volume") or 0.0),
                                "pnl": float(event.get("pnl") or 0.0),
                                "partial_realized_pnl": float(event.get("partial_realized_pnl") or 0.0),
                                "balance_before": balance_before,
                                "balance_after": float(balance),
                            },
                        )
                        self._log_trade_management_event(
                            "backtest_partial_close",
                            bar_ts_iso,
                            trade,
                            str(event.get("reason_code") or ReasonCode.PARTIAL_CLOSE.value),
                            {"volume": float(event.get("volume") or 0.0), "pnl": float(event.get("pnl") or 0.0)},
                        )
                    elif event["event_type"] == ManagementAction.MOVE_STOP.value:
                        recorder.event(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            event_type="trade_stop_moved",
                            payload={
                                "strategy_name": str(getattr(trade, "strategy_name", "")),
                                "setup_family": str(getattr(trade, "setup", "")),
                                "side": str(getattr(trade, "side", "")),
                                "reason_code": str(event.get("reason_code") or ReasonCode.STOP_MODIFIED.value),
                                "price": float(event.get("sl") or trade.sl),
                                "position_id": str(getattr(trade, "open_time", "")),
                            },
                        )
                        self._log_trade_management_event(
                            "backtest_move_stop",
                            bar_ts_iso,
                            trade,
                            str(event.get("reason_code") or ReasonCode.STOP_MODIFIED.value),
                            {"new_sl": float(event.get("sl") or trade.sl)},
                        )
                    elif event["event_type"] == ManagementAction.CLOSE_FULL.value:
                        payload = event["payload"]
                        open_ts = to_utc(payload["open_time"])
                        payload["duration_seconds"] = max((bar_ts - open_ts).total_seconds(), 0.0) if open_ts else 0.0
                        balance = recorder.persist_trade_close(
                            run_id=run_id,
                            bar_index=bar_index,
                            timestamp=bar_ts_iso,
                            trade_row=payload,
                            balance_before=float(balance),
                            equity_before=float(account.equity),
                            source="backtest",
                        )
                        account.balance = float(balance)
                        total_trade_pnl = float(payload.get("realized_pnl_total", payload.get("pnl", 0.0)) or 0.0)
                        account.consecutive_losses = account.consecutive_losses + 1 if total_trade_pnl < 0 else 0
                        self._mark_fingerprint_closed(
                            blocked_fingerprints,
                            str(payload.get("setup_fingerprint") or ""),
                            closed_at=bar_ts,
                            cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                            trade_id=str(payload.get("trade_id") or payload.get("position_id") or payload.get("open_time") or ""),
                        )
                        self._log_trade_management_event(
                            "backtest_close_full",
                            bar_ts_iso,
                            trade,
                            str(payload.get("exit_reason_code") or event.get("reason_code") or "close_full"),
                            {"exit_price": float(payload.get("exit_price") or current_close)},
                        )

                if trade not in executor.open_trades:
                    continue
                opened_at = to_utc(trade.open_time)
                if opened_at is None:
                    continue
                duration_minutes = (bar_ts - opened_at).total_seconds() / 60.0
                max_duration_minutes = self._max_duration_minutes_for_trade(trade)
                if duration_minutes >= max_duration_minutes:
                    payload = executor.close_trade_now(
                        trade=trade,
                        exit_price=self._current_exit_price(trade, current_close, executor),
                        exit_reason=ReasonCode.MAX_DURATION_EXIT.value,
                        close_time=bar_ts_iso,
                        reason_code=ReasonCode.MAX_DURATION_EXIT.value,
                    )
                    payload["duration_seconds"] = max((bar_ts - opened_at).total_seconds(), 0.0)
                    balance = recorder.persist_trade_close(
                        run_id=run_id,
                        bar_index=bar_index,
                        timestamp=bar_ts_iso,
                        trade_row=payload,
                        balance_before=float(balance),
                        equity_before=float(account.equity),
                        source="backtest",
                    )
                    account.balance = float(balance)
                    total_trade_pnl = float(payload.get("realized_pnl_total", payload.get("pnl", 0.0)) or 0.0)
                    account.consecutive_losses = account.consecutive_losses + 1 if total_trade_pnl < 0 else 0
                    self._mark_fingerprint_closed(
                        blocked_fingerprints,
                        str(payload.get("setup_fingerprint") or ""),
                        closed_at=bar_ts,
                        cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                        trade_id=str(payload.get("trade_id") or payload.get("position_id") or payload.get("open_time") or ""),
                    )
                    self._log_trade_management_event(
                        "backtest_max_duration_exit",
                        bar_ts_iso,
                        trade,
                        ReasonCode.MAX_DURATION_EXIT.value,
                        {"exit_price": float(payload.get("exit_price") or current_close)},
                    )

            closed = executor.on_bar(bar, bar_ts_iso)
            for trade_row in closed:
                open_ts = to_utc(trade_row["open_time"])
                duration = (bar_ts - open_ts).total_seconds() if open_ts else 0.0
                trade_row["duration_seconds"] = max(duration, 0.0)
                balance = recorder.persist_trade_close(
                    run_id=run_id,
                    bar_index=bar_index,
                    timestamp=bar_ts_iso,
                    trade_row=trade_row,
                    balance_before=float(balance),
                    equity_before=float(account.equity),
                    source="backtest",
                )
                account.balance = float(balance)
                total_trade_pnl = float(trade_row.get("realized_pnl_total", trade_row.get("pnl", 0.0)) or 0.0)
                account.consecutive_losses = account.consecutive_losses + 1 if total_trade_pnl < 0 else 0
                self._mark_fingerprint_closed(
                    blocked_fingerprints,
                    str(trade_row.get("setup_fingerprint") or ""),
                    closed_at=bar_ts,
                    cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                    trade_id=str(trade_row.get("trade_id") or trade_row.get("position_id") or trade_row.get("open_time") or ""),
                )

            self._refresh_backtest_account(
                account,
                executor,
                mark_price=current_close,
                now_utc=bar_ts,
                contract_size=float(symbol_spec["contract_size"]),
            )
            protection_reason = self._check_backtest_account_limits(account, protection_cfg)
            if protection_reason and not account.halted:
                account.halted = True
                account.halt_reason = protection_reason
                account.account_status = "ACCOUNT_BLOWN" if protection_reason == "account_blown" else "PROTECTION_STOPPED"
                account.blown_at_time = bar_ts_iso
                account.equity_at_stop = float(account.equity)
                recorder.event(
                    run_id=run_id,
                    bar_index=bar_index,
                    timestamp=bar_ts_iso,
                    event_type="account_protection_stop",
                    payload={
                        "reason_code": protection_reason,
                        "price": current_close,
                        "equity": float(account.equity),
                        "balance": float(balance),
                    },
                )
                for trade in list(executor.open_trades):
                    protection_payload = executor.close_trade_now(
                        trade=trade,
                        exit_price=self._current_exit_price(trade, current_close, executor),
                        exit_reason=protection_reason,
                        close_time=bar_ts_iso,
                        reason_code=protection_reason,
                        extra_metadata={"forced_close_reason": protection_reason},
                    )
                    protection_payload["duration_seconds"] = max(
                        (bar_ts - (to_utc(protection_payload["open_time"]) or bar_ts)).total_seconds(),
                        0.0,
                    )
                    balance = recorder.persist_trade_close(
                        run_id=run_id,
                        bar_index=bar_index,
                        timestamp=bar_ts_iso,
                        trade_row=protection_payload,
                        balance_before=float(balance),
                        equity_before=float(account.equity),
                        source="backtest",
                    )
                    account.balance = float(balance)
                    self._mark_fingerprint_closed(
                        blocked_fingerprints,
                        str(protection_payload.get("setup_fingerprint") or ""),
                        closed_at=bar_ts,
                        cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                        trade_id=str(protection_payload.get("trade_id") or protection_payload.get("position_id") or protection_payload.get("open_time") or ""),
                    )
                stop_processing_after_bar = True

            if stop_processing_after_bar:
                floating = executor.floating_pnl(float(bar["close"]))
                recorder.store_equity(
                    {"run_id": run_id, "ts": bar_ts_iso, "equity": balance + floating, "balance": balance, "floating_pnl": floating}
                )
                recorder.frame(
                    run_id=run_id,
                    bar_index=bar_index,
                    bar_ts_iso=bar_ts_iso,
                    bar=bar,
                    balance=balance,
                    executor=executor,
                    candidate=None,
                    candidate_summary={**candidate_summary, "halt_reason": account.halt_reason, "account_status": account.account_status},
                )
                break

            strategy_evaluation = strategy_evaluator.evaluate_setups(
                market=market,
                trend_slice=trend_slice,
                setup_slice=setup_slice,
                trigger_slice=trigger_slice,
                symbol_spec=symbol_spec,
            )
            raw_candidates = strategy_evaluation.raw_candidates
            candidates = strategy_evaluation.candidates
            candidate = strategy_evaluation.selected_candidate
            if not candidate:
                recorder.store_equity(
                    {
                        "run_id": run_id,
                        "ts": bar_ts_iso,
                        "equity": balance + executor.floating_pnl(float(bar["close"])),
                        "balance": balance,
                        "floating_pnl": executor.floating_pnl(float(bar["close"])),
                    }
                )
                recorder.event(
                    run_id=run_id,
                    bar_index=bar_index,
                    timestamp=bar_ts_iso,
                    event_type="candidate_rejected",
                    payload={
                        "reason_code": "no_candidate",
                        "strategy_name": "",
                        "setup_family": "",
                        "side": "",
                    },
                )
                recorder.frame(
                    run_id=run_id,
                    bar_index=bar_index,
                    bar_ts_iso=bar_ts_iso,
                    bar=bar,
                    balance=balance,
                    executor=executor,
                    candidate=None,
                    candidate_summary={**candidate_summary, "candidate_count": len(raw_candidates), "selected_family": None},
                )
                continue

            entry = strategy_evaluator.evaluate_entry(candidate, trigger_slice, symbol_spec, bar_ts)
            strategy_name = self._strategy_name_from_family(str(candidate["setup_family"]))
            setup_family = str(candidate["setup_family"])
            setup_fingerprint = str(candidate.get("setup_fingerprint") or "")
            decision = execution_router.decide(candidate=candidate, entry=entry, market_context=market)
            candidate_summary.update(
                {
                    "candidate_count": len(candidates),
                    "selected_family": setup_family,
                    "selected_side": str(candidate.get("side") or ""),
                    "selected_score": float(candidate.get("setup_score") or 0.0),
                    "entry_mode": str(decision.entry_mode or entry.get("entry_mode") or "observation_only"),
                    "execution_action": str(decision.action),
                    "decision_reason_code": str(decision.reason_code or ""),
                }
            )
            recorder.event(
                run_id=run_id,
                bar_index=bar_index,
                timestamp=bar_ts_iso,
                event_type="candidate_selected",
                payload={
                    "strategy_name": strategy_name,
                    "setup_family": setup_family,
                    "side": str(candidate.get("side") or ""),
                    "reason_code": str(decision.reason_code or "candidate_selected"),
                    "price": float(candidate.get("value_price") or candidate.get("entry_price") or 0.0),
                    "payload": self._compact_candidate(candidate),
                },
            )
            entry_mode = str(decision.entry_mode or entry.get("entry_mode") or "observation_only")
            is_limit_entry = decision.order_type == "limit"
            blocked_reason = None
            blocker_source = None
            executed = False
            signal_status = "rejected"
            trade_plan: dict[str, Any] | None = None
            final_entry_price = float(decision.entry_price or entry.get("pending_entry_price") or entry.get("entry_price") or 0.0)
            open_time_iso = bar_ts_iso
            final_volume = 0.0

            session_name = str(market["session"]["session_name"]).upper()
            fingerprint_guard_active = self._fingerprint_guard_applies(fingerprint_guard_cfg, setup_family)
            fingerprint_state = blocked_fingerprints.get(setup_fingerprint, {})
            cooldown_until = to_utc(fingerprint_state.get("cooldown_until")) if fingerprint_state.get("cooldown_until") else None
            if account.halted:
                blocked_reason = str(account.halt_reason or "account_halted")
                blocker_source = "account_protection"
                account.trades_after_stop_prevented += 1
            elif bool(weekend_cfg.get("enabled", True)) and self._is_friday_cutoff(bar_ts, friday_entry_cutoff):
                blocked_reason = "blocked_weekend_cutoff"
                blocker_source = "weekend_protection"
            elif session_filter_enabled and session_name not in allowed_sessions:
                blocked_reason = "blocked_bad_session"
                blocker_source = "session_gate"
            elif decision.action == BLOCK:
                blocked_reason = str(decision.reason_code or entry.get("reason_code") or "entry_not_ready")
                blocker_source = "entry_validation"
            elif fingerprint_guard_active and str(fingerprint_state.get("state") or "") == "active":
                blocked_reason = "blocked_duplicate_setup_fingerprint"
                blocker_source = "setup_fingerprint_guard"
            elif fingerprint_guard_active and cooldown_until is not None and bar_ts < cooldown_until:
                blocked_reason = "blocked_duplicate_setup_fingerprint"
                blocker_source = "setup_fingerprint_guard"
            else:
                accepted, conflict_reason = executor.can_accept_signal(
                    symbol=symbol,
                    side=str(candidate["side"]),
                    setup=setup_family,
                    setup_fingerprint=setup_fingerprint,
                    entry_mode=entry_mode,
                )
                if not accepted and conflict_reason == "pending_order_exists":
                    blocked_reason = "pending_order_exists"
                    blocker_source = "pending_order_conflict"
                elif not accepted:
                    blocked_reason = str(conflict_reason or "position_exists")
                    blocker_source = "exposure_conflict"
                else:
                    prev_attempt = last_attempt_at.get(setup_fingerprint)
                    if prev_attempt and (bar_ts - prev_attempt).total_seconds() < min_duplicate_gap:
                        blocked_reason = "duplicate_entry_blocked"
                        blocker_source = "duplicate_guard"

            spread_limit, _ = resolve_spread_limit(
                execution_cfg=self.config.get("execution", {}),
                setup_name=setup_family,
                regime_name=str(market.get("regime", {}).get("regime_name") or ""),
                fallback_limit=float(self.config.get("execution", {}).get("max_spread_points", 25.0)),
            )
            if blocked_reason is None and spread_points > spread_limit:
                blocked_reason = "spread_too_wide"
                blocker_source = "spread_gate"

            if blocked_reason is None:
                account.balance = float(balance)
                self._refresh_backtest_account(
                    account,
                    executor,
                    mark_price=current_close,
                    now_utc=bar_ts,
                    contract_size=float(symbol_spec["contract_size"]),
                )
                risk_decision = risk_engine.approve_order(
                    candidate=candidate,
                    entry={
                        **entry,
                        "entry_price": float(final_entry_price),
                        "pending_entry_price": float(final_entry_price),
                        "entry_mode": entry_mode,
                        "execution_decision": decision.action,
                        "quality_tier": decision.quality_tier,
                        "strategy_family": decision.strategy_family,
                        "management_profile": decision.management_profile,
                        "management_profile_key": decision.strategy_family.lower(),
                        "volatility_state": decision.metadata.get("volatility_state"),
                    },
                    market_context=market,
                    account_info=account,
                    symbol_spec=symbol_spec,
                    spread_points=spread_points,
                    live_requested=True,
                    reduced_risk=False,
                )
                trade_plan = risk_decision.metadata.get("trade_plan") if isinstance(risk_decision.metadata, dict) else None
                sizing = risk_decision.metadata.get("sizing", {}) if isinstance(risk_decision.metadata, dict) else {}
                if not risk_decision.approved or not trade_plan:
                    blocked_reason = str(risk_decision.reason_code or sizing.get("reason_code") or "risk_rejected")
                    blocker_source = "risk_sizing"
                else:
                    final_volume = float(risk_decision.volume)
                    projected_risk_pct = (float(sizing.get("risk_amount", 0.0) or 0.0) / max(float(balance), 1e-9)) * 100.0
                    exposure_decision = exposure_policy.evaluate(
                        candidate=candidate,
                        projected_risk_pct=projected_risk_pct,
                        open_exposures=self._open_exposures(executor, balance),
                        pending_exposures=self._pending_exposures(executor, balance),
                    )
                    if not exposure_decision.allowed:
                        blocked_reason = str(exposure_decision.reason_code or "blocked_portfolio_risk_cap")
                        blocker_source = "exposure_policy"
                        if isinstance(risk_decision.metadata, dict):
                            risk_decision.metadata["exposure_policy"] = exposure_decision.metadata
                    else:
                        entry["execution_decision"] = decision.action
                        entry["quality_tier"] = decision.quality_tier
                        entry["strategy_family"] = decision.strategy_family
                        entry["management_profile"] = decision.management_profile
                        entry["management_profile_key"] = trade_plan.get("management_profile_key")
                        entry["volatility_state"] = decision.metadata.get("volatility_state")
                        entry["entry_mode"] = entry_mode
                        entry["pending_expiry_minutes"] = decision.pending_expiry_minutes
                        entry["pending_expiry_bars"] = decision.pending_expiry_bars
                        entry["execution_reason_code"] = decision.reason_code
                        trade_plan.setdefault("management_profile", decision.management_profile)
                        trade_plan.setdefault("management_profile_key", trade_plan.get("management_profile_key") or decision.strategy_family.lower())
                        trade_plan.setdefault("quality_tier", decision.quality_tier)
                        trade_plan.setdefault("volatility_state", decision.metadata.get("volatility_state"))
                    if blocked_reason is None:
                        if is_limit_entry:
                            pending_expiry_minutes = int(entry.get("pending_expiry_minutes") or decision.pending_expiry_minutes or self.config.get("strategy", {}).get("lebprim_pending_expiry_minutes", 5))
                            pending_expiry_bars = int(entry.get("pending_expiry_bars") or decision.pending_expiry_bars or self.config.get("strategy", {}).get("lebprim_pending_expiry_bars", 3))
                            if bool(protection_cfg.get("enforce_margin", True)):
                                margin_result = risk_manager.fit_volume_to_margin(
                                    requested_volume=final_volume,
                                    account_info=account,
                                    symbol_spec=symbol_spec,
                                    margin_for_volume=lambda volume: account.required_margin(
                                        price=float(final_entry_price),
                                        volume=float(volume),
                                        contract_size=float(symbol_spec["contract_size"]),
                                    ),
                                )
                                if not bool(margin_result.get("valid")):
                                    blocked_reason = "insufficient_margin"
                                    blocker_source = "margin_protection"
                                    account.rejected_insufficient_margin_count += 1
                                else:
                                    final_volume = float(margin_result.get("volume", final_volume) or final_volume)
                            if blocked_reason is None:
                                plan = CanonicalExecutionPlan(
                                    strategy_name=strategy_name,
                                    symbol=symbol,
                                    side=str(candidate["side"]),
                                    order_type="limit",
                                    entry_price=float(final_entry_price),
                                    volume=final_volume,
                                    stop_loss=float(trade_plan["stop_loss"]),
                                    take_profit=float(trade_plan["tp2"]),
                                    setup_family=setup_family,
                                    setup_fingerprint=setup_fingerprint,
                                    entry_mode=entry_mode,
                                    execution_model=execution_model,
                                    signal_time=bar_ts_iso,
                                    execution_time=open_time_iso,
                                    signal_bar_time=bar_ts_iso,
                                    signal_bar_close=current_close,
                                    execution_bar_time=open_time_iso,
                                    executable_entry=float(final_entry_price),
                                    spread_used=float(spread_points),
                                    slippage_used=float(self._slippage_points(slippage_model)),
                                    fill_side="ask" if str(candidate["side"]).upper() == "LONG" else "bid",
                                    data_available_through_time=bar_ts_iso,
                                    tp1=float(trade_plan["tp1"]),
                                    trigger_price=float(entry.get("trigger_price") or entry.get("entry_price") or bar["close"]),
                                    metadata={
                                        "pending_expiry_bars": pending_expiry_bars,
                                        "pending_expiry_minutes": pending_expiry_minutes,
                                        "placed_bar_index": bar_index,
                                        "run_id": run_id,
                                        "execution_decision": decision.action,
                                        "execution_reason_code": decision.reason_code,
                                        "quality_tier": decision.quality_tier,
                                        "strategy_family": decision.strategy_family,
                                        "management_profile": decision.management_profile,
                                        "pending_entry_price": float(final_entry_price),
                                        "sl": float(trade_plan["stop_loss"]),
                                        "tp": float(trade_plan["tp2"]),
                                        "volume": final_volume,
                                        "created_at_ts": bar_ts.timestamp(),
                                        "candidate": candidate,
                                        "entry": entry,
                                        "market": market,
                                        "anchor_time": candidate.get("anchor_time"),
                                    },
                                )
                                execution_result = execution_service.submit_order(
                                    build_execution_request_from_plan(plan)
                                )
                            else:
                                execution_result = {"ok": False, "reason": blocked_reason}
                            pending_order_id = str(execution_result.get("order_id") or "")
                            if not execution_result.get("ok"):
                                blocked_reason = str(execution_result.get("reason") or "pending_order_submit_failed")
                                blocker_source = "execution_service"
                            else:
                                recorder.event(
                                    run_id=run_id,
                                    bar_index=bar_index,
                                    timestamp=bar_ts_iso,
                                    event_type="pending_created",
                                    payload={
                                        "strategy_name": strategy_name,
                                        "setup_family": setup_family,
                                        "side": str(candidate["side"]),
                                        "reason_code": ReasonCode.ORDER_PENDING.value,
                                        "price": float(final_entry_price),
                                        "signal_id": pending_order_id,
                                    },
                                )
                                self.logger.structured(
                                    "backtest_trade_opened",
                                    {
                                        "timestamp": bar_ts_iso,
                                        "side": str(candidate["side"]),
                                        "entry": float(final_entry_price),
                                        "sl": float(trade_plan["stop_loss"]),
                                        "tp": float(trade_plan["tp2"]),
                                        "setup_family": setup_family,
                                        "reason_code": ReasonCode.ORDER_PENDING.value,
                                        "order_id": pending_order_id,
                                    },
                                )
                                executed = False
                                signal_status = "pending"
                        else:
                            execution_bar_time = bar_ts_iso
                            executable_entry = float(final_entry_price)
                            if execution_model == "next_bar_open":
                                next_bar = clock.peek_next_bar()
                                if not next_bar:
                                    blocked_reason = "no_next_bar_for_fill"
                                    blocker_source = "fill_model"
                                else:
                                    final_entry_price = float(next_bar["open"])
                                    next_bar_ts = to_utc(next_bar["time"])
                                    open_time_iso = next_bar_ts.isoformat() if next_bar_ts else bar_ts_iso
                                    execution_bar_time = open_time_iso
                                    executable_entry = float(final_entry_price)
                            elif execution_model == "current_bar_close":
                                final_entry_price = float(current_close)
                                open_time_iso = bar_ts_iso
                                execution_bar_time = bar_ts_iso
                                executable_entry = float(final_entry_price)
                            elif not (float(bar["low"]) <= float(final_entry_price) <= float(bar["high"])):
                                blocked_reason = "signal_price_not_touched"
                                blocker_source = "fill_model"

                            if blocked_reason is None:
                                if bool(protection_cfg.get("enforce_margin", True)):
                                    margin_result = risk_manager.fit_volume_to_margin(
                                        requested_volume=final_volume,
                                        account_info=account,
                                        symbol_spec=symbol_spec,
                                        margin_for_volume=lambda volume: account.required_margin(
                                            price=float(final_entry_price),
                                            volume=float(volume),
                                            contract_size=float(symbol_spec["contract_size"]),
                                        ),
                                    )
                                    if not bool(margin_result.get("valid")):
                                        blocked_reason = "insufficient_margin"
                                        blocker_source = "margin_protection"
                                        account.rejected_insufficient_margin_count += 1
                                    else:
                                        final_volume = float(margin_result.get("volume", final_volume) or final_volume)
                            if blocked_reason is None:
                                rebased = risk_manager.rebase_trade_levels(trade_plan, final_entry_price, symbol_spec)
                                plan = CanonicalExecutionPlan(
                                    strategy_name=strategy_name,
                                    symbol=symbol,
                                    side=str(candidate["side"]),
                                    order_type="market",
                                    entry_price=float(rebased["entry_price"]),
                                    volume=final_volume,
                                    stop_loss=float(rebased["stop_loss"]),
                                    take_profit=float(rebased["tp2"]),
                                    setup_family=setup_family,
                                    setup_fingerprint=setup_fingerprint,
                                    entry_mode=entry_mode,
                                    execution_model=execution_model,
                                    signal_time=bar_ts_iso,
                                    execution_time=open_time_iso,
                                    signal_bar_time=bar_ts_iso,
                                    signal_bar_close=current_close,
                                    execution_bar_time=execution_bar_time,
                                    executable_entry=executable_entry,
                                    spread_used=float(spread_points),
                                    slippage_used=float(self._slippage_points(slippage_model)),
                                    fill_side="ask" if str(candidate["side"]).upper() == "LONG" else "bid",
                                    data_available_through_time=bar_ts_iso,
                                    tp1=float(rebased["tp1"]),
                                    trigger_price=float(entry.get("trigger_price") or entry.get("entry_price") or bar["close"]),
                                    metadata={
                                        "run_id": run_id,
                                        "execution_decision": decision.action,
                                        "execution_reason_code": decision.reason_code,
                                        "quality_tier": decision.quality_tier,
                                        "strategy_family": decision.strategy_family,
                                        "management_profile": decision.management_profile,
                                        "anchor_time": candidate.get("anchor_time"),
                                        "candidate": candidate,
                                        "entry": entry,
                                        "market": market,
                                    },
                                )
                                execution_result = execution_service.submit_order(
                                    build_execution_request_from_plan(plan)
                                )
                                if not execution_result.get("ok"):
                                    blocked_reason = str(execution_result.get("reason") or "market_order_submit_failed")
                                    blocker_source = "execution_service"
                                else:
                                    trade = executor.open_trades[-1]
                                    trade.metadata.setdefault("trade_id", str(setup_fingerprint or open_time_iso))
                                    trade.metadata.setdefault("position_id", str(getattr(trade, "open_time", "")))
                                    trade_plan = {
                                        "entry": trade.entry,
                                        "sl": trade.sl,
                                        "tp": trade.tp,
                                        "tp1": trade.tp1,
                                        "volume": trade.volume,
                                    }
                                    recorder.event(
                                        run_id=run_id,
                                        bar_index=bar_index,
                                        timestamp=bar_ts_iso,
                                        event_type="trade_opened",
                                        payload={
                                            "strategy_name": strategy_name,
                                            "setup_family": setup_family,
                                            "side": str(candidate["side"]),
                                            "reason_code": "executed_simulated",
                                            "price": float(trade.entry),
                                            "position_id": str(getattr(trade, "open_time", "")),
                                            "signal_bar_time": bar_ts_iso,
                                            "execution_bar_time": execution_bar_time,
                                            "execution_model": execution_model,
                                            "executable_entry": executable_entry,
                                            "spread_used": float(spread_points),
                                            "slippage_used": float(self._slippage_points(slippage_model)),
                                            "fill_side": "ask" if str(candidate["side"]).upper() == "LONG" else "bid",
                                        },
                                    )
                                    self._mark_fingerprint_executed(blocked_fingerprints, setup_fingerprint, str(getattr(trade, "open_time", "")))
                                    self.logger.structured(
                                        "backtest_trade_opened",
                                        {
                                            "timestamp": open_time_iso,
                                            "side": str(candidate["side"]),
                                            "entry": float(trade.entry),
                                            "sl": float(trade.sl),
                                            "tp": float(trade.tp),
                                            "setup_family": setup_family,
                                            "reason_code": "executed_simulated",
                                        },
                                    )
                                    executed = True
                                    signal_status = "filled"

                    if blocked_reason is None and not is_limit_entry and executed is False and signal_status != "pending":
                        signal_status = "filled"

            execution_reason = None
            if blocked_reason is None and is_limit_entry and signal_status == "pending":
                execution_reason = ReasonCode.ORDER_PENDING.value
            elif blocked_reason is None and signal_status == "filled":
                execution_reason = "executed_simulated" if execution_model != "next_bar_open" or is_limit_entry else "executed"
            elif blocked_reason is None:
                execution_reason = "executed"
            else:
                execution_reason = None

            last_attempt_at[setup_fingerprint] = bar_ts
            if blocked_reason == "position_exists":
                conflict_trade = next(
                    (
                        trade
                        for trade in executor.open_trades
                        if str(trade.symbol).upper() == symbol and not getattr(trade, "closed", False)
                    ),
                    None,
                )
                if conflict_trade is not None:
                    self._log_trade_management_event(
                        "backtest_position_exists_block",
                        bar_ts_iso,
                        conflict_trade,
                        "position_exists",
                        {"candidate_setup_family": setup_family, "candidate_side": str(candidate["side"])},
                    )

            recorder.store_signal(
                {
                    "run_id": run_id,
                    "signal_time": bar_ts_iso,
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "side": str(candidate["side"]),
                    "setup": setup_family,
                    "entry": float(entry.get("entry_price") or entry.get("trigger_price") or 0.0),
                    "sl": float(trade_plan.get("sl", trade_plan.get("stop_loss"))) if trade_plan else None,
                    "tp": float(trade_plan.get("tp", trade_plan.get("tp2"))) if trade_plan else None,
                    "score": float(entry.get("entry_score") or candidate.get("setup_score") or 0.0),
                    "executed": bool(executed),
                    "execution_reason": execution_reason,
                    "blocked_reason": blocked_reason,
                    "raw_signal_json": {
                        "candidate": candidate,
                        "entry": {**entry, "entry_mode": entry_mode},
                        "market": market,
                        "execution_decision": {
                            "action": decision.action,
                            "reason_code": decision.reason_code,
                            "reason": decision.reason,
                            "entry_price": decision.entry_price,
                            "reference_price": decision.reference_price,
                            "pending_expiry_minutes": decision.pending_expiry_minutes,
                            "pending_expiry_bars": decision.pending_expiry_bars,
                            "metadata": decision.metadata,
                        },
                        "diagnostics": {
                            "setup_valid": bool(candidate.get("setup_valid")),
                            "trigger_score": entry.get("trigger_score"),
                            "entry_score": entry.get("entry_score"),
                            "entry_mode": entry_mode,
                            "pending_entry_price": entry.get("pending_entry_price"),
                            "blocker_source": blocker_source,
                            "blocked_reason": blocked_reason,
                            "signal_status": signal_status,
                            "quality_tier": decision.quality_tier,
                            "management_profile": decision.management_profile,
                            "original_trade_id": fingerprint_state.get("original_trade_id"),
                            "cooldown_until": fingerprint_state.get("cooldown_until"),
                        },
                    },
                }
            )
            playback_signal_event = "signal_blocked" if blocked_reason else "signal_detected"
            recorder.event(
                run_id=run_id,
                bar_index=bar_index,
                timestamp=bar_ts_iso,
                event_type=playback_signal_event,
                payload={
                    "strategy_name": strategy_name,
                    "setup_family": setup_family,
                    "side": str(candidate["side"]),
                    "reason_code": str(blocked_reason or execution_reason or "signal_detected"),
                    "price": float(entry.get("entry_price") or entry.get("trigger_price") or final_entry_price or 0.0),
                    "signal_id": str(candidate.get("setup_fingerprint") or ""),
                },
            )
            floating = executor.floating_pnl(float(bar["close"]))
            recorder.store_equity(
                {"run_id": run_id, "ts": bar_ts_iso, "equity": balance + floating, "balance": balance, "floating_pnl": floating}
            )
            recorder.frame(
                run_id=run_id,
                bar_index=bar_index,
                bar_ts_iso=bar_ts_iso,
                bar=bar,
                balance=balance,
                executor=executor,
                candidate=candidate,
                candidate_summary={
                    **candidate_summary,
                    "blocked_reason": blocked_reason,
                    "execution_reason": execution_reason,
                    "signal_status": signal_status,
                },
            )

        if clock.get_current_bar() is not None and executor.open_trades:
            last_bar = clock.get_current_bar()
            close_ts = to_utc(last_bar["time"]) or utc_now()
            for trade in list(executor.open_trades):
                trade_row = trade.close_full(
                    exit_price=float(last_bar["close"]),
                    ts_iso=close_ts.isoformat(),
                    exit_reason="FORCED_END_OF_TEST",
                    reason_code=ReasonCode.FORCED_END_OF_TEST.value,
                    contract_size=float(symbol_spec["contract_size"]),
                    commission=executor._commission_for_trade(trade),
                    swap=float(getattr(trade, "metadata", {}).get("swap", 0.0) or 0.0),
                )
                trade_row["duration_seconds"] = max((close_ts - (to_utc(trade.open_time) or close_ts)).total_seconds(), 0.0)
                recorder.event(
                    run_id=run_id,
                    bar_index=bar_index,
                    timestamp=close_ts.isoformat(),
                    event_type="forced_end_of_test",
                    payload={
                        "strategy_name": str(trade_row.get("strategy_name", "")),
                        "setup_family": str(trade_row.get("setup_family", getattr(trade, "setup", ""))),
                        "side": str(trade_row.get("side", "")),
                        "reason_code": ReasonCode.FORCED_END_OF_TEST.value,
                        "price": float(trade_row.get("exit_price") or last_bar["close"]),
                        "position_id": str(trade_row.get("position_id") or getattr(trade, "open_time", "")),
                        "pnl": float(trade_row.get("final_realized_pnl", trade_row.get("pnl", 0.0)) or 0.0),
                    },
                )
                balance = recorder.persist_trade_close(
                    run_id=run_id,
                    bar_index=bar_index,
                    timestamp=close_ts.isoformat(),
                    trade_row=trade_row,
                    balance_before=float(balance),
                    equity_before=float(account.equity),
                    source="backtest",
                )
                self._mark_fingerprint_closed(
                    blocked_fingerprints,
                    str(trade_row.get("setup_fingerprint") or ""),
                    closed_at=close_ts,
                    cooldown_minutes=int(fingerprint_guard_cfg.get("cooldown_minutes_after_close", 30) or 30),
                    trade_id=str(trade_row.get("trade_id") or trade_row.get("position_id") or trade_row.get("open_time") or ""),
                )
            executor.open_trades = []

        self.database.update_backtest_run(
            run_id,
            {
                "metadata_json": {
                    "requested_execution_mode": requested_engine_mode or resolved_mode_summary.get("selected_mode"),
                    "resolved_execution_mode": resolved_mode_summary,
                    "enabled_strategies_effective": list(selected_strategies),
                    "history_resolution": history_resolution.source_details,
                    "requested_start": start_dt.isoformat(),
                    "requested_end": end_dt.isoformat(),
                    "effective_data_start": effective_replay_first.isoformat() if effective_replay_first else None,
                    "effective_data_end": effective_replay_last.isoformat() if effective_replay_last else None,
                    "first_bar_time": effective_first_bar.isoformat() if effective_first_bar else None,
                    "last_bar_time": effective_last_bar.isoformat() if effective_last_bar else None,
                    "data_source": history_resolution.source_kind,
                    "data_truncated_reason": (
                        "requested_end_after_available_data"
                        if effective_replay_last is not None and effective_replay_last < end_dt
                        else None
                    ),
                    "account_status": account.account_status,
                    "blown_at_time": account.blown_at_time,
                    "equity_at_stop": account.equity_at_stop,
                    "trades_after_stop_prevented": int(account.trades_after_stop_prevented),
                    "rejected_insufficient_margin_count": int(account.rejected_insufficient_margin_count),
                }
            },
        )
        bundle = self.storage.get_run_bundle(run_id)
        try:
            final_progress = progress_total if "progress_total" in locals() else 1
            summary_state = JobState.COMPLETED.value if str((bundle.get("summary") or {}).get("reconciliation_status")) == "ok" else JobState.PARTIAL.value
            self.database.update_backtest_run_state(
                run_id,
                summary_state,
                progress_current=final_progress,
                progress_total=final_progress,
            )
            exports = self.storage.export_run_bundle(run_id, self.base_dir)
            bundle["artifacts"] = exports
            self.database.update_backtest_run_state(
                run_id,
                summary_state,
                progress_current=final_progress,
                progress_total=final_progress,
                artifacts=exports,
            )
            self.logger.info(
                f"BACKTEST artifacts exported | run_id={run_id} | path={exports.get('run_dir', '')}"
            )
        except Exception as exc:
            bundle["artifacts_error"] = str(exc)
            self.database.update_backtest_run_state(
                run_id,
                JobState.PARTIAL.value,
                progress_current=progress_total if "progress_total" in locals() else 1,
                progress_total=progress_total if "progress_total" in locals() else 1,
                failure_reason=f"artifact_export_failed: {exc}",
            )
            self.logger.warning(f"BACKTEST artifact export failed | run_id={run_id} | error={exc}")
        return bundle
