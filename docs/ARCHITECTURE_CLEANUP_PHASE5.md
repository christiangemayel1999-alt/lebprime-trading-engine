# Architecture Cleanup Phase 5

Phase 5 performed controlled, statically provable consolidation. It did not change trading calculations, requests, lifecycle semantics, persistence, or runtime ordering.

## Safe consolidations completed

1. Every live context-only call continues through `LiveContextProvider` / `UnifiedStrategyContextAdapter`. `main.py` now imports and calls `build_live_runtime_support_engine()` by its real name rather than disguising it as the generic strategy builder.
2. `LiveMT5Feed` accepts `MarketDataGateway` and no longer imports `MT5Connector`. The same connector instance can still be passed structurally and the same `fetch_rates()` call is made.
3. `LebPrimeLiveService.connectors` was removed after repository-wide tracing found no caller. `broker_gateways` is the sole cache.
4. LebPrime trade and performance reads were confirmed to have one canonical implementation in `LebPrimeLiveRepository`. Thin service methods remain boundary delegates.

No files, APIs, configuration keys, or metric implementations were removed.

## Remaining strategy dependency

The live runtime has no direct `self.strategy` call for dataframe preparation, session classification, or market-context analysis. Remaining references are:

- class-name logging during startup/reload;
- `generate_setup_candidates()`;
- `choose_best_setup()`;
- `evaluate_entry()` for live and non-live profiles;
- the optional shadow runner's full-engine dependency.

Those methods belong to the surviving generic fallback/shadow path. Removing `build_live_runtime_support_engine()` would remove behavior, so it remains.

## LebPrime constructor compatibility

`broker_gateway_factory` is canonical. The `connector_factory` parameter remains deprecated compatibility because the current compatibility suite constructs the service with it extensively. The unused `self.connector_factory` storage is retained with that contract; removing the parameter or storage should be one coordinated migration. The `connectors` attribute alias had no repository caller and was removed.

## Direct MT5Connector classification

| Consumer | Classification | Decision |
|---|---|---|
| `main.py` | `KEEP DIRECT` | Process composition root plus generic lifecycle/health surface uses connector-specific state and operations. LebPrime receives gateways from here. |
| `scripts/bootstrap_launcher.py` | `KEEP DIRECT` | Startup diagnostics intentionally validate connector initialization, terminal state, and executable discovery before runtime composition. |
| `services/backtest_runner.py` | `NEEDS SPECIALIZED ADAPTER` | Historical acquisition needs initialization, date-range rates, symbol specification, and shutdown; the current live market-data protocol is insufficient. |
| `services/indicator_lab/dataset_service.py` | `NEEDS SPECIALIZED ADAPTER` | Reuses historical range loading and permits connector injection; it should share a future historical-data source contract with backtests. |
| `services/live_feed.py` | `USE GATEWAY — COMPLETED` | Requires only `fetch_rates()` and now accepts `MarketDataGateway`. |
| `services/manual_trade_service.py` | `NEEDS SPECIALIZED ADAPTER` | Uses market-order execution, MT5 enums, connection stages, normalization, position lookup, and modification behavior not represented by the LebPrime pending-order gateway. |
| `trading_bot/execution/mt5_broker.py` | `KEEP DIRECT` | This is itself the generic execution adapter and currently assembles generic requests. Merging it with the LebPrime gateway lacks request-parity evidence. |

## Dashboard and performance read side

The canonical LebPrime read path is:

```text
lebprime_broker_entries / lebprime_live_zones
  -> LebPrimeLiveRepository dashboard snapshots
  -> LebPrimeLiveService thin delegates
  -> /api/trades/lebprime and /api/performance/lebprime
  -> frontend
```

The repository alone calculates complete/incomplete outcomes, broker-authoritative PnL, and symbol/timeframe/entry breakdowns. Generic `DashboardDataService` and `dashboard/queries.py` calculations remain because `/api/status`, Logs, Health, and compatibility consumers use different generic tables and semantics. They are not duplicate LebPrime projections.

No deprecated API passed all deletion gates. The production frontend does not call the old strategy/session/symbol/risk/execution/status/list routes, but `PROJECT_CONTEXT.md` and compatibility contracts still publish them. Manual-operation APIs are intentionally retained even though they are absent from navigation.

## Configuration cleanup

No alias passed the removal gate. Research/backtest paths still consume generic strategy/session/regime/entry/symbol/timeframe configuration, and schema normalization still creates compatibility views. Static tracing cannot prove that stored configs, worker payloads, imports/exports, or external scripts have migrated.

## Risk and execution decisions

- `RiskManager`, `RiskEngine`, and `LebPrimeRiskService` remain separate. Their scopes differ and no behavior-equivalence evidence supports a merge.
- `DynamicPositionSizer` is `KEEP`: it is optional and non-authoritative for LebPrime, but active when enabled in the generic fallback path.
- `MT5Connector`, `MT5Broker`, and `MT5BrokerGateway` remain separate. LebPrime pending-order construction must not be moved through `MT5Broker` without field-, enum-, retry-, idempotency-, and lifecycle-parity evidence.
- `PositionManager` is `DEPRECATED`: it has no production/research caller and only an architecture contract test. Phase 6 may remove it together with that explicit contract after confirming it is not planned public API.

## Phase 6 readiness

| Component | Status | Evidence / prerequisite |
|---|---|---|
| `build_live_runtime_support_engine` | `KEEP` | Required by generic fallback and shadow/full-engine calls. |
| `UnifiedStrategyContextAdapter` | `KEEP` | Canonical live context boundary. |
| `connector_factory` compatibility alias | `DEPRECATED` | Extensively used by compatibility tests; migrate callers before removal. |
| `connectors` compatibility alias | `READY TO REMOVE` | Removed in Phase 5 after zero repository callers were found. |
| `MT5BrokerGateway` | `KEEP` | Canonical delegation boundary for LebPrime. |
| `MT5Broker` | `KEEP` | Active generic/manual adapter; request semantics differ. |
| `MT5Connector` | `KEEP` | Proven low-level MT5 and broker-normalization implementation. |
| `RiskManager` | `KEEP` | Authoritative risk implementation. |
| `RiskEngine` | `KEEP` | Active structured generic facade. |
| `LebPrimeRiskService` | `KEEP` | Narrow live dependency boundary. |
| `DynamicPositionSizer` | `KEEP` | Called by enabled generic fallback execution and close tracking. |
| `PositionManager` | `DEPRECATED` | No non-test caller; Phase 6 removal candidate after public-contract decision. |
| Deprecated dashboard APIs | `DEPRECATED` | No production UI callers, but still documented/contracted. |
| Legacy config aliases | `UNKNOWN` | External/stored-config use cannot be disproved statically. |
| Duplicate dashboard read helpers | `KEEP` | Generic status and LebPrime broker projections are semantically distinct; no duplicate LebPrime calculator remains. |

Phase 6 should validate recorded broker-request parity before any execution merge, instrument config/API compatibility use before deletion, and decide the public status of `PositionManager`. No production validation is implied by this static cleanup.
