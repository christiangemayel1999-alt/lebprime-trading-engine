# Phase 5 Consolidation Plan

Phase 5 should consolidate only where the Phase 4 contracts can preserve byte-for-byte request intent or equivalent persisted outcomes. The statuses below apply to the current repository, not an aspirational redesign.

| Component | Classification | Rationale / next requirement |
|---|---|---|
| `RiskManager` | `KEEP SEPARATE` | It is the implementation owner for gates, sizing, margin fitting, and generic management. It now structurally satisfies `LebPrimeRiskService`; merging it into a facade adds no value. |
| `RiskEngine` | `KEEP SEPARATE` | It composes generic spread/quality approval around `RiskManager`; LebPrime intentionally calls the narrower risk service after strategy intent exists. |
| `DynamicPositionSizer` | `REMOVE LATER` | It overlaps percentage adjustment and is not authoritative for LebPrime. Confirm whether analytics/shadow users need it, then remove or explicitly designate it as advisory. |
| `MT5Connector` | `KEEP SEPARATE` | Proven low-level MT5 implementation and normalization boundary. Higher layers should adapt it, not absorb it. |
| `MT5Broker` | `NEEDS ADAPTER FIRST` | Generic `ExecutionRequest` adapter reimplements parts of pending request assembly. Compare its produced request and result contract with `MT5BrokerGateway`/connector calls before consolidation. |
| `ReplayBroker` | `KEEP SEPARATE` | Deterministic bar simulation is intentionally different from live account/history capabilities. Share only small execution/state protocols. |
| `UnifiedStrategyEngine` | `KEEP SEPARATE` | Active backtest/research engine and temporary legacy fallback; it must not become the LebPrime live owner. |
| `strategy_factory` | `KEEP SEPARATE` | Historical mode selection remains active. Live support construction cannot be removed until the generic fallback path is retired. |
| `LebprimStrategy` | `KEEP SEPARATE` | Active research/backtest adapter implementation; not the production v3.4.0 engine. Rename/relocate only after external compatibility review. |
| `LiveContextProvider` | `READY TO MERGE` | All clearly contextual live call sites already delegate to the adapter with identical arguments and return values. Broader use remains behavior-equivalent because the adapter performs direct forwarding and exposes no execution methods. |
| `LebPrimeLiveService` | `KEEP SEPARATE` | Canonical owner of LebPrime zones, pending semantics, retries, reconciliation, and lifecycle persistence. |
| `LivePositionLifecycleManager` | `NEEDS ADAPTER FIRST` | Owns generic positions while LebPrime owns its broker lifecycle. A shared lifecycle event contract and attribution rules are required before merging. |
| `TradeJournalService` | `KEEP SEPARATE` | Cross-domain durable event sink with retry/outbox behavior. It should not be merged into a strategy repository. |
| `LebPrimeLiveRepository` | `KEEP SEPARATE` | Strategy-specific lifecycle/write and dashboard projection owner. It consumes the shared journal but is not a replacement for it. |
| Deprecated dashboard APIs | `REMOVE LATER` | Migrate documented clients and update `PROJECT_CONTEXT.md`; retain until external compatibility is explicitly retired. |
| Legacy config aliases | `UNKNOWN` | Instrument actual access from persisted configs, workers, scripts, and imports before classifying individual aliases for removal. |

## Ready-to-merge equivalence evidence

Only the context boundary is marked ready. `UnifiedStrategyContextAdapter` forwards the same engine instance, arguments, and return values for dataframe preparation, session classification, and context analysis. It holds no state beyond the engine reference and cannot generate candidates or place trades. The runtime swap/rollback path replaces or restores the engine and adapter together.

The MT5 gateway is safe as an introduced boundary but is not authorization for merging `MT5Broker` with the LebPrime flow. Its methods delegate directly, yet the two higher-level flows assemble different inputs and results.

## Required evidence before larger merges

- Static request-field mapping for every pending order field and MT5 enum.
- Recorded parity for broker-normalized entry, SL, TP, volume, expiry, filling mode, comment, and magic number.
- Persisted lifecycle parity across acceptance, fill, partial close, full close, cancellation, expiration, rejection, and restart.
- Risk-decision parity for permission, volume, margin fitting, exposure, and drawdown locks.
- Explicit netting/hedging attribution behavior.
- Migration plan for documented APIs and stored configuration aliases.

Phase 4 provides dependency seams only. It does not provide production or broker validation for a Phase 5 merge.
