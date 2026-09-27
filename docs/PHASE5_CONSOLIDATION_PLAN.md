# Phase 5 Consolidation Record

Phase 5 consolidated only where the Phase 4 contracts preserve the same calls and outcomes. The statuses below describe the resulting repository, not an aspirational redesign.

| Component | Classification | Rationale / next requirement |
|---|---|---|
| `RiskManager` | `KEEP SEPARATE` | It is the implementation owner for gates, sizing, margin fitting, and generic management. It now structurally satisfies `LebPrimeRiskService`; merging it into a facade adds no value. |
| `RiskEngine` | `KEEP SEPARATE` | It composes generic spread/quality approval around `RiskManager`; LebPrime intentionally calls the narrower risk service after strategy intent exists. |
| `DynamicPositionSizer` | `KEEP SEPARATE` | It is not authoritative for LebPrime, but the enabled generic fallback path calls it for risk scaling and close-result updates. |
| `MT5Connector` | `KEEP SEPARATE` | Proven low-level MT5 implementation and normalization boundary. Higher layers should adapt it, not absorb it. |
| `MT5Broker` | `NEEDS ADAPTER FIRST` | Generic `ExecutionRequest` adapter reimplements parts of pending request assembly. Compare its produced request and result contract with `MT5BrokerGateway`/connector calls before consolidation. |
| `ReplayBroker` | `KEEP SEPARATE` | Deterministic bar simulation is intentionally different from live account/history capabilities. Share only small execution/state protocols. |
| `UnifiedStrategyEngine` | `KEEP SEPARATE` | Active backtest/research engine and temporary legacy fallback; it must not become the LebPrime live owner. |
| `strategy_factory` | `KEEP SEPARATE` | Historical mode selection remains active. Live support construction cannot be removed until the generic fallback path is retired. |
| `LebprimStrategy` | `KEEP SEPARATE` | Active research/backtest adapter implementation; not the production v3.4.0 engine. Rename/relocate only after external compatibility review. |
| `LiveContextProvider` | `READY TO MERGE — COMPLETED` | All contextual live call sites delegate to the adapter with identical arguments and return values. `main.py` now also names the dedicated support builder explicitly. |
| `LebPrimeLiveService` | `KEEP SEPARATE` | Canonical owner of LebPrime zones, pending semantics, retries, reconciliation, and lifecycle persistence. |
| `LivePositionLifecycleManager` | `NEEDS ADAPTER FIRST` | Owns generic positions while LebPrime owns its broker lifecycle. A shared lifecycle event contract and attribution rules are required before merging. |
| `TradeJournalService` | `KEEP SEPARATE` | Cross-domain durable event sink with retry/outbox behavior. It should not be merged into a strategy repository. |
| `LebPrimeLiveRepository` | `KEEP SEPARATE` | Strategy-specific lifecycle/write and dashboard projection owner. It consumes the shared journal but is not a replacement for it. |
| Deprecated dashboard APIs | `REMOVE LATER — COMPLETED IN PHASE 6` | Caller tracing found no supported HTTP clients; documentation/tests were migrated and the aliases were removed. |
| Legacy config aliases | `UNKNOWN` | Instrument actual access from persisted configs, workers, scripts, and imports before classifying individual aliases for removal. |

## Completed safe consolidations

- `LiveContextProvider` remains the sole context path in the live runtime. The only remaining `self.strategy` method calls generate/select/evaluate candidates in the surviving generic fallback.
- `LiveMT5Feed` now depends on `MarketDataGateway`; its runtime object and `fetch_rates()` call are unchanged.
- The unused `LebPrimeLiveService.connectors` attribute alias was removed. `broker_gateways` is canonical.
- `connector_factory` was retained during Phase 5; Phase 6 migrated the internal tests to `broker_gateway_factory` and removed the alias after finding no runtime or documented external caller.
- LebPrime trade/performance projections were confirmed canonical in `LebPrimeLiveRepository`; no second metric implementation was introduced.

The MT5 gateway is safe as an introduced boundary but is not authorization for merging `MT5Broker` with the LebPrime flow. Its methods delegate directly, yet the two higher-level flows assemble different inputs and results.

## Required evidence before larger merges

- Static request-field mapping for every pending order field and MT5 enum.
- Recorded parity for broker-normalized entry, SL, TP, volume, expiry, filling mode, comment, and magic number.
- Persisted lifecycle parity across acceptance, fill, partial close, full close, cancellation, expiration, rejection, and restart.
- Risk-decision parity for permission, volume, margin fitting, exposure, and drawdown locks.
- Explicit netting/hedging attribution behavior.
- Migration plan for documented APIs and stored configuration aliases.

Neither Phase 4 nor Phase 5 provides production or broker validation for a larger merge.
