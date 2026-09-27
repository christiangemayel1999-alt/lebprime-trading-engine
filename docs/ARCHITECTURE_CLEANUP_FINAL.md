# Final Architecture Cleanup

This is the frozen architecture state after Phases 1–6. Automated validation is comprehensive but is not production or live-broker validation.

## Canonical paths

```text
Market data
  -> LiveContextProvider / UnifiedStrategyContextAdapter (read-only context)
  -> LiveLebPrimeEngine / LebPrimeLiveService (zones and Entry1/Entry2)
  -> LebPrimeRiskService implemented by RiskManager
  -> LebPrimeBrokerGateway
  -> MT5BrokerGateway
  -> MT5Connector
  -> MT5

MT5 state/history
  -> BrokerGateway
  -> LebPrime reconciliation
  -> LebPrimeLiveRepository / lebprime_broker_entries
  -> TradeJournalService

Repository/database
  -> canonical dashboard snapshots
  -> API
  -> frontend
```

`LebPrimeLiveService` is the sole production LebPrime signal-to-pending-order owner. `LiveContextProvider` cannot generate candidates or authorize/place orders. Telegram remains notification/control-plane infrastructure and is never execution authority.

Research and backtest remain separate:

```text
BacktestRunner / Indicator Lab / OOS
  -> strategy_factory.build_strategy_engine()
  -> UnifiedStrategyEngine or historical engine
  -> ReplaySession / ReplayBroker / simulated account
  -> BacktestStorage / DatabaseService
  -> Research APIs
```

Replay cannot enter the live LebPrime execution query and does not implement meaningless live account/history capabilities.

## Ownership

| Concern | Final owner |
|---|---|
| LebPrime strategy truth, zones, Entry1/Entry2, structural SL/TP | `LiveLebPrimeEngine` / `LebPrimeLiveService` |
| Read-only session/regime/context | `LiveContextProvider` / `UnifiedStrategyContextAdapter` |
| Risk permission, sizing, margin fit | `RiskManager` through `LebPrimeRiskService` |
| Generic structured risk facade | `RiskEngine` |
| Broker normalization, filling mode, stop validation, order check/send | `MT5Connector` below `MT5BrokerGateway` |
| Broker lifecycle truth | MT5 state/history reconciled by `LebPrimeLiveService` |
| Durable LebPrime lifecycle projection | `LebPrimeLiveRepository` / `lebprime_broker_entries` |
| Shared lifecycle journal | `TradeJournalService` |
| LebPrime Trades/Performance projections | `LebPrimeLiveRepository.dashboard_*_snapshot()` |
| Generic health/status projection | `DashboardDataService` and `dashboard/queries.py` |

Requested strategy values remain distinct from broker-normalized and broker-result fields. Complete/incomplete outcome handling and broker-authoritative PnL definitions are unchanged.

## Compatibility and endpoint decisions

Removed in Phase 6:

- `trading_bot/positions/manager.py` and its architecture-only test;
- `connector_factory` and the earlier `connectors` compatibility names;
- old strategy/family/risk/execution/session/symbol dashboard routes and their unused request schemas;
- old `/api/strategies/lebprime-v3-4-0/status`;
- duplicate singular `GET /api/backtests` listing.

No deprecated FastAPI route remains. Manual-operation endpoints remain supported backend operations. Generic fields in `/api/status` remain because current Overview/Logs/Health and generic runtime consumers use that payload; they are not duplicate LebPrime performance calculations.

Legacy configuration is retained where direct readers exist. This includes setup controls/families, `strategy.lebprim_*`, session/regime/entry research settings, execution-mode normalization, and normalized stored-config aliases. No configuration key was removed in Phase 6.

## Direct MT5Connector dependencies

| Consumer | Decision |
|---|---|
| `main.py` | Keep: process composition, health, generic lifecycle, and gateway construction. |
| `scripts/bootstrap_launcher.py` | Keep: pre-runtime terminal/readiness diagnostics. |
| `services/backtest_runner.py` | Keep direct for historical range acquisition; a new adapter would not yet remove construction/lifecycle logic. |
| `services/indicator_lab/dataset_service.py` | Keep: delegates historical loading to the backtest loader and permits connector injection. |
| `services/manual_trade_service.py` | Keep: specialized MT5 enum, normalization, market-order, and position-management needs. |
| `trading_bot/execution/mt5_broker.py` | Keep: generic broker adapter with semantics distinct from LebPrime pending placement. |
| `trading_bot/execution/mt5_gateway.py` | Keep: canonical LebPrime delegation adapter. |

`LiveMT5Feed` remains migrated to `MarketDataGateway`. No `HistoricalMarketDataGateway` was introduced because it would not remove the concrete construction and lifecycle shared by the two research acquisition paths.

## Component inventory

| Component | Classification | Reason |
|---|---|---|
| `main.py` | `ACTIVE CORE` | Runtime composition and orchestration. |
| `strategy.py` | `COMPATIBILITY` | Generic fallback and historical engine. |
| `strategy_factory.py` | `ACTIVE CORE` | Explicit live-support and research engine construction. |
| `lebprim_strategy.py` | `RESEARCH ONLY` | Compatibility/backtest LEBPRIM strategy, not live v3.4.0. |
| `indicators.py` | `RESEARCH ONLY` | Generic engine/backtest indicators. |
| `trading_bot/strategy/` | `ACTIVE CORE` | Context boundary plus research/shadow engines. |
| `services/lebprime_live/` | `ACTIVE CORE` | Canonical live strategy, broker lifecycle, and persistence. |
| `MT5Connector` | `ACTIVE CORE` | Low-level MT5 and broker normalization. |
| `MT5Broker` | `COMPATIBILITY` | Active generic/manual execution adapter. |
| `MT5BrokerGateway` | `ACTIVE CORE` | Canonical LebPrime broker boundary. |
| `RiskManager` | `ACTIVE CORE` | Authoritative risk implementation. |
| `RiskEngine` | `COMPATIBILITY` | Active generic structured facade. |
| `LebPrimeRiskService` | `ACTIVE CORE` | Narrow live risk contract. |
| `DynamicPositionSizer` | `COMPATIBILITY` | Optional generic fallback feature; not LebPrime authority. |
| `PositionManager` | `REMOVED` | No supported caller or public contract. |
| `TradeJournalService` | `ACTIVE CORE` | Shared durable lifecycle journal. |
| `LebPrimeLiveRepository` | `ACTIVE CORE` | Canonical LebPrime lifecycle and dashboard projection. |
| dashboard legacy routes | `REMOVED` | No supported HTTP caller after canonical API migration. |
| deprecated `GET /api/backtests` | `REMOVED` | Duplicate of `/api/backtest/runs`. |
| legacy config aliases | `COMPATIBILITY` | Active readers or un-migrated stored-config normalization. |

## Validation and database state

The final suite completed with 302 passed, 0 failed, 0 skipped, and 17 Python deprecation warnings in 214.33 seconds. All 149 tracked Python sources compiled; dashboard JavaScript passed `node --check`; safe core imports, merge-marker, deleted-import, route-caller, and `git diff --check` scans passed.

The configured database was opened read-only. Its persisted schema predates 15 lifecycle fields, so no live data was modified. The real legacy table definition and one sampled row were cloned into a temporary database; `LebPrimeLiveRepository` added every required field, preserved the row, and a second initialization made no duplicate change. Migrations are additive and guarded by column existence. The next normal repository initialization applies them to the configured database.

`.env`, database, storage, and log artifacts remain ignored and untracked. No credential-like value was added. `config.json.bak-*` is now ignored to prevent accidental backup staging.

## Intentionally separate components

- `RiskManager`, `RiskEngine`, and `LebPrimeRiskService` have different authority/facade/contract roles.
- `MT5Connector`, `MT5Broker`, and `MT5BrokerGateway` have different low-level, generic, and LebPrime execution semantics.
- `ReplayBroker` remains deterministic simulation rather than pretending to expose live account/history capabilities.
- Generic lifecycle/status compatibility remains separate from broker-authoritative LebPrime reconciliation and performance.

Remaining debt is limited to compatibility config instrumentation/migration, Python `datetime.utcnow()` deprecation warnings, the older configured database awaiting its normal additive initialization, and future parity evidence before any generic/LebPrime execution merge.
