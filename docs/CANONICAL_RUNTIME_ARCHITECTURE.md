# Canonical Runtime Architecture

This document describes the code paths that exist after Phase 3. It distinguishes the production LebPrime v3.4.0 execution path from the compatibility strategy engine used for shared context, diagnostics, and research.

## Production startup and live path

```text
TradingBot startup
  -> load persisted config + synchronize control-state mode
  -> initialize logger, state, DatabaseService, heartbeat, TradeJournalService
  -> initialize config/control/operator/Telegram command services
  -> initialize TelegramNotifier and MT5Connector
  -> wrap MT5Connector with MT5Broker + ExecutionService (generic/manual path)
  -> build live runtime support UnifiedStrategyEngine (legacy families disabled)
  -> initialize optional analytics/shadow/correlation services
  -> initialize RiskManager + RiskEngine
  -> initialize LebPrimeLiveService
  -> initialize LivePositionLifecycleManager (generic position lifecycle)
  -> acquire the single-instance lock

live cycle
  -> support-engine market/session diagnostics
  -> LebPrimeLiveService.scan_once()
  -> LiveLebPrimeEngine consumes closed broker candles
  -> zones and Entry1/Entry2 are persisted by LebPrimeLiveRepository
  -> LebPrime preflight uses account/symbol/risk state
  -> MT5Connector builds/checks/sends pending broker orders
  -> reconciliation reads current orders, positions, order history, and deal history
  -> broker lifecycle is persisted in lebprime_broker_entries
  -> TradeJournalService records idempotent lifecycle events
  -> TelegramNotifier emits notifications independently of execution authority
  -> dashboard LebPrime APIs read repository projections only
```

`LebPrimeLiveService` is the only production LebPrime v3.4.0 signal-to-pending-order path. `UnifiedStrategyEngine` is still constructed in live mode because `main.py` uses shared context/diagnostic interfaces, but `build_live_runtime_support_engine()` disables its setup families in a private configuration copy. It does not replace or authorize LebPrime v3.4.0 broker execution.

## Lifecycle and persistence ownership

| Concern | Canonical owner | Notes |
|---|---|---|
| LebPrime zones and Entry1/Entry2 | `services/lebprime_live/service.py` + `LiveLebPrimeEngine` | Closed-candle/Pine-parity path. |
| Pending request construction and broker checks | `LebPrimeLiveService` calling `MT5Connector` | Uses connector normalization, `order_check`, and `order_send` wrappers. |
| LebPrime idempotency | `LebPrimeLiveRepository` entry key plus service reservation/retry flow | Prevents duplicate placement across cycles/restarts. |
| Broker lifecycle reconciliation | `LebPrimeLiveService._reconcile_broker_execution()` | Broker orders, positions, order history, and position deal history are authoritative. |
| LebPrime lifecycle write model | `lebprime_broker_entries` through `LebPrimeLiveRepository` | Single durable per-entry projection; no dashboard writes. |
| Trade/event journal | `TradeJournalService` + `DatabaseService` | Shared journal with deterministic event keys. |
| Trades/Performance read models | `LebPrimeLiveRepository.dashboard_*_snapshot()` | Exclude historical rows and incomplete outcomes from PnL arithmetic. |
| Generic/manual execution | `ExecutionService` + `MT5Broker` + `MT5Connector` | Retained for current generic/manual callers; not the LebPrime pending placement owner. |
| Generic position management | `LivePositionLifecycleManager` | Retained for the generic execution path and existing live lifecycle contracts. |

## Dependency map

| Module/group | Live | Backtest / replay / research | Dashboard / worker / other | Decision |
|---|---|---|---|---|
| `strategy.py` | Instantiated by the live support registry | V1 baseline and unified legacy adapter | Tests and compatibility docs | Keep. |
| `strategy_factory.py` | `build_live_runtime_support_engine()` from `main.py` | `build_strategy_engine()` from `BacktestRunner` | Execution-mode contract tests | Keep; its split responsibilities are already explicit. |
| `lebprim_strategy.py` | Instantiated by `LebprimStrategyAdapter` inside the live support registry; families are disabled for live execution | Active unified/backtest `LEBPRIM_SCALP` implementation | Direct behavioral tests | Keep. |
| `indicators.py` | Used by both compatibility strategy implementations | Active backtest/research dependency | — | Keep. |
| `trading_bot/strategy/` | Support engine, diagnostics, shadow testing | Unified engine, adapters, validation, replay diagnostics | — | Keep entire active graph. |
| `trading_bot/execution/` | Canonical decisions/exposure and generic/manual broker adapter | replay broker, pending policy, canonical plan | manual service | Keep active modules; isolated stubs were removed. |
| `trading_bot/risk/` | `RiskEngine`, management profiles, optional dynamic sizer | `RiskEngine` in backtest/replay | — | Keep. |
| `trading_bot/positions/live_lifecycle.py` | Generic live reconciliation/management | Contract tests | — | Keep. |
| `trading_bot/positions/manager.py` | No current production caller | — | Architecture contract tests | Keep pending a broader position-model decision. |
| `services/lebprime_live/` | Canonical LebPrime production service/repository/symbol/timeframe path | Historical rows are explicitly non-executable | dashboard constructs a read-only service instance | Keep. |
| `services/backtest_runner.py` | — | Canonical backtest orchestration | Worker jobs | Keep. |
| `services/backtest_replay.py` | — | Replay clock, data, evaluator, account, recorder, and session | Backtest runner | Keep. |
| `services/backtest_storage.py` | — | Persists bundles/frames/events and supports OOS | Dashboard and worker | Keep. |
| `services/database.py` | Runtime state and journal persistence | Backtest/OOS/Indicator Lab persistence | Dashboard and worker | Keep. |
| `services/trade_journal.py` | Canonical journal facade | Shared where runtime-compatible events are written | scripts/manual service | Keep. |

## Backtest path

The Research UI queues `POST /api/jobs/backtest`. `JobService` persists the job and the worker invokes `BacktestRunner`. The runner builds a mode-appropriate engine through `strategy_factory.build_strategy_engine()`, uses `ReplaySession` and `ReplayBroker` for deterministic execution, and stores run bundles plus playback frames/events through `BacktestStorage` and `DatabaseService`.

The canonical list/detail API is `GET /api/backtest/runs` and `GET /api/backtest/run/{run_id}`. The deprecated `GET /api/backtests` wrapper delegates to the same list projection because it is still documented externally.

## Replay path

Replay is persisted playback, not live execution. `BacktestRunner` produces frames and events using the replay classes in `services/backtest_replay.py`; `DashboardDataService` reads them from `DatabaseService`. The Research UI consumes `/api/backtests/{run_id}/replay/*` and `/stream/*`. No replay row can enter the live LebPrime execution query.

## Indicator Lab path

Dashboard routes call `IndicatorLabService`, which coordinates dataset and campaign services backed by `DatabaseService`. Worker jobs run campaign work. This path evaluates frozen data and does not call `LebPrimeLiveService` or send MT5 orders.

## Worker path

`scripts/run_worker.py` starts `WorkerService`. The worker constructs its own config/database, OOS, Indicator Lab, job, backtest runner, and storage services. It processes persisted research jobs and publishes heartbeat state read by the dashboard. It is not a live broker execution worker.

## Compatibility components retained

- Deprecated generic strategy/family/risk/execution/session/symbol routes because `PROJECT_CONTEXT.md` documents them.
- Deprecated old LebPrime status route because a contract test still requires it.
- Deprecated `/api/backtests` because it remains a documented compatibility endpoint.
- Generic trade fields in `/api/status` because Logs/Health and compatibility consumers still use portions of the status payload.
- `StrategyEngine`, `LebprimStrategy`, adapters, and configuration aliases because backtest/research and the live support engine still instantiate them.
- Generic `ExecutionService`, `MT5Broker`, and `LivePositionLifecycleManager` because manual/generic runtime paths still call them.
