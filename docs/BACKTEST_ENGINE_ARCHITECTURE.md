# Backtest Engine Architecture

Date: 2026-09-08

## Before

`BacktestRunner.run()` was the primary owner for nearly every replay responsibility. One method validated the request, resolved execution mode, loaded history, prepared timeframes, advanced the replay cursor, sliced visible candles, called the strategy engine, evaluated risk and exposure, submitted canonical orders, processed pending orders, managed open positions, updated simulated account state, wrote playback frames/events/signals/trades, reconciled results, exported artifacts, and updated job state.

That made behavior easy to accidentally couple across unrelated concerns. For example, changing playback writes required reading through fill timing, pending-order activation, account protection, and strategy-selection logic in the same method.

## Responsibility Map

The original runner flow decomposes into these logical blocks:

1. Request validation and date parsing.
2. Execution-mode resolution and replay-only config overrides.
3. Strategy selection and strategy-engine refresh.
4. Risk, execution, exposure, pending-policy, and executor setup.
5. Historical-data resolution from MT5/cache/CSV sources.
6. Timeframe preparation and replay-window selection.
7. Replay progress and heartbeat updates.
8. Per-bar no-lookahead candle visibility.
9. Market context and strategy candidate evaluation.
10. Entry decision, session/weekend/spread/duplicate gates.
11. Risk sizing, exposure approval, and canonical execution-plan submission.
12. Pending-order cancellation, expiry, activation, and recording.
13. Open-position management, TP1/TP2/SL/max-duration/friday/account-protection closes.
14. Account/equity/floating-PnL refresh.
15. Signal, trade, playback frame, playback event, and equity persistence.
16. End-of-test forced close and reconciliation.
17. Artifact export and final job-state update.

Dependencies flow mostly forward: request/config selection influences strategy/risk/execution services; historical data feeds the replay clock and market windows; market windows feed strategy/risk/lifecycle evaluation; execution results update executor/account state; recorder persists the observed results.

## After

`BacktestRunner` remains the public facade. External dashboard/API callers still construct and call `BacktestRunner.run(request, ...)` the same way. The runner now owns request/config/data preparation and hands replay execution to focused replay components in `services/backtest_replay.py`.

```text
BacktestRunner
|
`-- ReplaySession
    |
    |-- ReplayContext
    |-- ReplayClock
    |-- ReplayMarketData
    |-- StrategyReplayEvaluator
    |-- ReplayRecorder
    `-- SimulatedAccount
```

The main behavior-preserving boundary is `ReplaySession`: it coordinates one historical replay using the same existing strategy engine, `RiskManager`, `RiskEngine`, `ExecutionDecisionEngine`, `CanonicalExecutionPlan`, `ExecutionService`, `ReplayBroker`, and `SimulatedExecutor` path as before.

## Data Flow

```text
Historical Data
-> Replay Clock
-> Market Window
-> Strategy
-> Risk
-> Canonical Execution
-> Replay Broker
-> Lifecycle
-> Account
-> Recorder
```

## Components

`BacktestRunner`:
Validates input, resolves execution mode, applies replay config, loads historical bars, prepares strategy dataframes, creates execution services, creates `ReplayContext`, and returns the session result.

`ReplayContext`:
Typed container for one replay run's dependencies and immutable setup values. It carries run ID, request/config, symbol, selected strategies, data-source metadata, clock, market data, symbol spec, execution model, policies, executor, services, and protection/session settings. It does not implement business logic.

`ReplayClock`:
Owns bar progression and progress count. It advances exactly one historical bar at a time and exposes `peek_next_bar()` for the existing `next_bar_open` fill model.

`ReplayMarketData`:
Owns legal candle windowing. At timestamp `T`, trigger bars are visible through `T`, while setup/trend bars are visible only once their higher-timeframe bar duration has fully elapsed.

`StrategyReplayEvaluator`:
Owns the replay-specific strategy call sequence: market-context analysis, setup generation, selected-family filtering, best-candidate selection, diagnostics logging, and entry evaluation. It calls the existing strategy engine and does not duplicate formulas.

`ReplayRecorder`:
Owns replay persistence calls for equity, signals, playback frames, playback events, pending-resolution signals, and trade closes. It preserves the existing storage schema and delegates to existing storage/helper paths.

`SimulatedAccount`:
Owns replay account state fields and refresh calculations while using `SimulatedExecutor.floating_pnl()` as the authoritative floating-PnL source.

## Shared LIVE/BACKTEST Components

The refactor keeps these shared or behavior-authoritative components in the backtest path:

- Existing strategy engines from `strategy.py`, `lebprim_strategy.py`, and `trading_bot/strategy/`.
- `RiskManager` and `RiskEngine`.
- `ExecutionDecisionEngine`.
- `CanonicalExecutionPlan`.
- `build_execution_request_from_plan()`.
- `ExecutionService`.
- Exposure and pending policy objects.

LIVE continues to use `ExecutionService -> MT5Broker -> MT5Connector`. BACKTEST continues to use `ExecutionService -> ReplayBroker -> SimulatedExecutor`.

## Backtest-Only Components

Replay-specific infrastructure is isolated under `services/backtest_replay.py`:

- `ReplaySession`
- `ReplayContext`
- `ReplayClock`
- `ReplayMarketData`
- `ReplayWindow`
- `StrategyReplayEvaluator`
- `StrategyEvaluationResult`
- `ReplayRecorder`
- `SimulatedAccount`

These components do not connect to MT5 live trading or place orders.

## State Ownership

- Clock: `ReplayClock`.
- Current legal candle windows: `ReplayMarketData` and `ReplayWindow`.
- Run setup/dependencies: `ReplayContext`.
- Simulated account fields: `SimulatedAccount`.
- Pending orders: still behaviorally owned by `SimulatedExecutor` and policy decisions from `PendingOrderPolicy`, coordinated by `ReplaySession`.
- Open positions: still behaviorally owned by `SimulatedExecutor`, with management decisions from `RiskManager`, coordinated by `ReplaySession`.
- Recorder/storage writes: `ReplayRecorder`.
- Run state and final artifact status: `BacktestRunner` creates the run; `ReplaySession` finalizes it through existing database/storage services.

## Determinism

The no-lookahead contract is preserved by `ReplayMarketData.window_at()`. A replay bar at timestamp `T` receives:

- trigger slice: rows with `time <= T`
- setup slice: rows with `time + setup_duration <= T`
- trend slice: rows with `time + trend_duration <= T`

The strategy evaluator receives only those slices. The golden replay fixture was not changed.

## Future TradingView Parity Hooks

A future TradingView comparison layer can attach at these seams without changing trading behavior:

- after `ReplayMarketData.window_at()` to compare visible OHLC windows,
- after `StrategyReplayEvaluator.evaluate_setups()` to compare candidate/entry diagnostics,
- after canonical plan construction to compare intent-level fields,
- after `ReplayRecorder` writes playback frames/events to export comparison artifacts.

This refactor does not implement TradingView parity.
