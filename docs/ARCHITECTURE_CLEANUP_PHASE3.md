# Architecture Cleanup Phase 3

## 1. Files removed

- `filters.py`: `TradeFilters` had no live, backtest/replay, Indicator Lab, worker, dashboard, Telegram, test, or documented caller.
- `trading_bot/execution/orders.py`: unused `OrderRequest` dataclass with no caller or documented contract.
- `trading_bot/execution/sim_broker.py`: unused `SimBroker` alias with no caller or test; research uses `ReplayBroker` directly.
- `trading_bot/positions/exits.py`: unused reason-code aliases with no caller.
- `trading_bot/positions/lifecycle.py`: unused `LifecycleEvent` dataclass with no caller.

## 2. Functions and classes removed

- `TradeFilters`, `OrderRequest`, `SimBroker`, and `LifecycleEvent` through the file removals above.
- Unused dashboard queries `get_trades_by_day()` and `get_win_loss_counts()`.
- Unused `DashboardDataService.list_oos_runs()`; OOS routes use `OOSService.history()`.
- Unreferenced JavaScript helpers: `replaySerializeCandidate`, `replaySetSelection`, `replayViewportContainsBar`, `replayPanViewport`, `replayPause`, `replayDrawNavigator`, and `refreshExecutionMode`.

## 3. APIs removed

None. Each Phase 2 deprecated route still has either a documented compatibility reference, an architectural contract test, or insufficient evidence about external callers.

## 4. APIs consolidated

`GET /api/backtest/runs` is the canonical run-list endpoint used by Research. Both it and deprecated `GET /api/backtests` now delegate to one `backtest_runs_payload()` projection; the deprecated wrapper adds only its legacy `backtest` configuration field. Detail remains `GET /api/backtest/run/{run_id}`, while replay/stream stays under `/api/backtests/{run_id}/...` because the current UI and contract tests call it.

## 5. Modules retained and why

- `strategy.py`: live support registry, V1 backtests, and unified legacy adapter.
- `strategy_factory.py`: separate live support and historical/backtest constructors.
- `lebprim_strategy.py`: active `LebprimStrategyAdapter` dependency in `UnifiedStrategyEngine`, including backtests; also instantiated by the live support engine with families disabled.
- `indicators.py`: shared by both compatibility strategy implementations.
- `trading_bot/strategy/`: active registry, adapters, engine, validation, diagnostics, and shadow testing.
- Active `trading_bot/execution/` modules: used by main, manual execution, backtest, and replay.
- `trading_bot/positions/live_lifecycle.py`: active generic live lifecycle; `manager.py` remains because architecture tests encode its contract.
- `services/lebprime_live/`, database, journal, backtest, replay, storage, Indicator Lab, OOS, job, and worker services: all have current runtime consumers.

## 6. `main.py` simplifications

Initialization order and objects are unchanged, but two independent construction blocks are now named methods:

- `_initialize_analytics_services()` builds decision logging, R-multiple calculation, shadow testing, correlation monitoring, and optional dynamic sizing.
- `_initialize_live_execution_services()` wires `RiskManager`, `RiskEngine`, `LebPrimeLiveService`, `JournalRepository`, and `LivePositionLifecycleManager`.

Configuration loading, mode synchronization, Telegram construction, MT5 construction, startup logging, flags, and instance-lock timing remain in their previous order.

## 7. Risk duplication findings

`risk_manager.py` is the implementation owner for risk gates, SL/TP trade plans, fixed-fractional sizing, state locks, and management actions. `trading_bot/risk/engine.py` is a structured approval facade that composes `RiskManager` with spread and quality adjustments; main and backtest/replay use it. `management_profiles.py` is called by `RiskManager` for family-aware exit behavior. `position_sizing.py` is an optional analytics-era dynamic percentage calculator constructed by main when enabled, but it is not the authoritative LebPrime lot-sizing path.

The overlap is conceptual rather than a safe duplicate deletion: percentage adjustment exists in both `RiskManager`/`RiskEngine` and `DynamicPositionSizer`, while `RiskEngine` wraps rather than replaces `RiskManager`. Phase 3 did not unify them because doing so could change volume or exit behavior.

## 8. Execution duplication findings

- `MT5Connector` owns MT5 connection/symbol normalization, request construction helpers, precheck, `order_check`, `order_send`, cancellation, position modification/close, and broker history wrappers.
- `services/execution_flow.py` owns shared pure decisions such as final-outcome normalization, duplicate/cooldown classification, and spread-limit resolution.
- `trading_bot/execution/` owns generic canonical plans, decision/exposure/pending policies, broker abstraction, live MT5 adapter, and replay adapter.
- `LebPrimeLiveService` owns LebPrime Entry1/Entry2 reservations, idempotency, paired pending placement semantics, retry policy, and broker reconciliation.

There is overlap between `MT5Broker._submit_limit()` and the direct LebPrime connector flow, but centralizing it would risk changing the proven pending-order sequence and idempotency. It remains a Phase 4 design task.

## 9. Configuration usage findings

The live LebPrime source is `lebprime_v3_4_0`; generic `strategy.lebprim_*`, `setup_controls`, and `setup_families` belong to compatibility/research engines. `regime`, `sessions`, `entry`, and `indicators` remain active in support/backtest strategy paths. `analytics` and `position_sizing` are optional main-runtime facilities. No persisted key was proven unused strongly enough to delete. See `CONFIG_USAGE_MAP.md` for exact consumers and classification.

## 10. Unresolved legacy components

- `UnifiedStrategyEngine` always registers both legacy and Lebprim adapters, even when live support configuration disables all families; it still uses them for shared context/features.
- Generic/manual execution and the canonical LebPrime direct connector path coexist.
- Generic lifecycle/status data coexists with the LebPrime broker-entry read model.
- Deprecated dashboard APIs remain externally documented.
- `PositionManager` is test-backed but has no current production import.
- `DynamicPositionSizer` overlaps conceptually with authoritative risk sizing but is optional and still constructed by main.

## 11. Phase 4 recommendations

1. Add an explicit support-context interface so live startup need not instantiate candidate-producing strategy adapters merely to classify session/context.
2. Define one broker gateway contract that can preserve the exact LebPrime pending sequence before attempting to merge `MT5Broker` and direct connector calls.
3. Decide whether manual/generic execution remains a supported operator feature; only then retire its service/lifecycle stack.
4. Introduce typed configuration views and instrument compatibility-key access before migrating aliases.
5. Migrate documented clients from deprecated routes, update `PROJECT_CONTEXT.md`, then remove those routes in a separately versioned API change.
6. Decide whether `DynamicPositionSizer` is a reporting experiment or an execution component; eliminate or integrate it only with explicit lot-size parity evidence.
7. Reassess `PositionManager` after its architecture contract is either adopted by live code or formally retired.

## 12. Intentionally not changed

No signal generation, Pine parity, zone, Entry1/Entry2, SL/TP, risk percentage, lot-sizing, pending-order, broker reconciliation, demo guard, Telegram independence, symbol mapping, timeframe construction, or replay non-execution behavior was changed. There were no database migrations, broker actions, test trades, fabricated zones, or automatic merges.

## Verification boundary

No pytest, integration, frontend, smoke, or broker test suite was run. Validation was limited to Python compilation for edited Python modules, JavaScript syntax checking, repository-wide caller/import searches, static inspection, and `git diff --check`.
