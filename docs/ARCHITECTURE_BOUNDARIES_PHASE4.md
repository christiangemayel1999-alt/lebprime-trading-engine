# Architecture Boundaries Phase 4

Phase 4 introduces narrow structural contracts around existing behavior. It does not replace the strategy engine, risk implementation, connector, repository, journal, or replay broker.

## 1. `LiveContextProvider`

`trading_bot.strategy.live_context.LiveContextProvider` exposes only the contextual calls currently made by the live runtime:

- `prepare_trend_dataframe()`
- `prepare_setup_dataframe()`
- `prepare_trigger_dataframe()`
- `classify_session()`
- `analyze_market_context()`

It deliberately has no candidate generation, candidate selection, entry evaluation, risk, execution, or broker methods.

## 2. Context adapter and live wiring

`UnifiedStrategyContextAdapter` delegates those five operations to the already-constructed live support engine. `TradingBot.live_context` now owns all contextual calls, including startup/diagnostic session classification and dataframe/context construction. Config reload builds the replacement adapter before swapping the engine and restores both engine and adapter on failure.

`TradingBot.strategy` remains temporarily because the generic fallback, shadow runner, and diagnostics still call candidate-generation interfaces. With `lebprime_v3_4_0` configured, `TradingBot.run_cycle()` executes `LebPrimeLiveService.scan_once()` and returns before that fallback candidate path. Removing the fallback engine is a Phase 5 decision, not a Phase 4 assumption.

## 3. Broker gateway contract

`trading_bot.execution.gateway` divides the exact LebPrime capabilities into small structural protocols:

| Protocol | Capabilities |
|---|---|
| `MarketDataGateway` | symbol selection, rates, tick, symbol specification, spread-stage result |
| `AccountGateway` | account/terminal information and broker margin calculation |
| `PendingOrderGateway` | pending enum, request construction, precheck, send, cancellation |
| `BrokerStateGateway` | current pending orders, bot positions, ticket/comment position lookup |
| `BrokerHistoryGateway` | bot order/deal history and complete position-deal history |
| `PositionManagementGateway` | partial and full position close used by existing lifecycle events |
| `LebPrimeBrokerGateway` | combined live-service contract plus connection lifecycle and MT5 constants |

The combined contract is intentionally specific to the current LebPrime service. Replay can implement shared smaller protocols without pretending to support MT5 account/history capabilities.

## 4. MT5 adapter

`MT5BrokerGateway` is a delegation-only adapter around `MT5Connector`. It does not normalize, rebuild, reinterpret, retry, or serialize broker operations. Request construction, filling-mode choice, stop adjustment, margin calls, `order_check`, `order_send`, demo/account data, and history remain in the proven connector.

`LebPrimeLiveService` accepts `broker_gateway_factory`. The previous `connector_factory` parameter and `connectors` attribute remain compatibility aliases for existing callers and fakes. Main uses `broker_gateway_factory`; the service no longer imports or names `MT5Connector`.

## 5. LebPrime dependency direction

```text
LiveLebPrimeEngine
  -> immutable-in-practice zone output (strategy entry, SL, TP1, TP2)
  -> LebPrimeLiveService
       -> LebPrimeRiskService (permission, volume, margin fit)
       -> LebPrimeBrokerGateway
            -> MT5BrokerGateway
                 -> MT5Connector
       <- broker result/state/history
       -> LebPrimeLiveRepository
       -> TradeJournalService

LebPrimeLiveRepository -> dashboard snapshots -> HTTP read APIs -> frontend
```

The dashboard cannot write lifecycle state or invoke the gateway. Repository and notifier remain concrete dependencies because adding interfaces for them would not currently reduce a competing implementation or unsafe coupling.

## 6. Risk ownership

| Responsibility | Current authoritative owner | Boundary note |
|---|---|---|
| 1. Account-level trading permission | `RiskManager.evaluate_risk_gate()` plus LebPrime demo-account validation | Demo validation remains a broker/account safety gate, not generic risk math. |
| 2. Structured generic risk approval | `RiskEngine.approve_order()` | Composes spread/quality checks with `RiskManager`; generic and replay paths use it. |
| 3. Structural SL/TP generation | `LiveLebPrimeEngine` for live v3.4.0; `RiskManager.calculate_trade_levels()` for generic strategies | LebPrime strategy SL/TP is not moved into generic risk. |
| 4. Lot sizing | `RiskManager.calculate_position_size()` | `DynamicPositionSizer` is optional and not authoritative for LebPrime. |
| 5. Margin validation | `RiskManager.fit_volume_to_margin()` using `BrokerGateway.calculate_margin()` | Risk chooses an acceptable size; connector calculates broker margin. |
| 6. Minimum broker stop validation | `MT5Connector.build_pending_limit_request()` / connector stop validation | Broker-normalized SL is stored separately from strategy SL. |
| 7. Exposure limits | `ExposurePolicy` for generic execution; LebPrime repository/idempotency and current-position checks for its path | Not yet one unified exposure model. |
| 8. Drawdown locks | `RiskManager.evaluate_risk_gate()` and persisted runtime risk state | Shared state is passed into LebPrime. |
| 9. Open-position management | `LivePositionLifecycleManager` for generic positions; `LebPrimeLiveService` for its zone events and broker lifecycle | Keep separate until ownership is reconciled. |
| 10. Partial close / breakeven / trailing | `RiskManager` management profiles plus `LivePositionLifecycleManager` for generic trades; LebPrime service follows its existing engine events | No behavior moved in Phase 4. |

`LebPrimeRiskService` formalizes only the three calls LebPrime currently makes: account risk gate, position size, and margin fitting. `RiskManager` conforms structurally; no adapter or calculation rewrite is involved.

## 7. Trade intent versus broker request

No new duplicate trade model was added.

- Generic execution already has `CanonicalExecutionPlan`, converted to `ExecutionRequest` at the broker boundary.
- LebPrime strategy intent is the engine zone plus its Entry1/Entry2 values. The broker-entry row preserves requested `entry_price`, strategy zone SL/TP, identity, and metadata before broker normalization.
- The broker request is created later by `MT5Connector.build_pending_limit_request()` and persisted with separate normalized price, submitted SL, volume, ticket, and retcode fields.
- Broker result/history is persisted as fill/close price, realized PnL, tickets, timestamps, and terminal reason.

`CanonicalExecutionPlan` is not adopted for LebPrime in Phase 4 because it does not natively encode timeframe, zone ID, entry number, dual targets, or the requested-versus-normalized distinction. A future typed `LebPrimeTradeIntent` should replace—not duplicate—the existing zone-to-entry payload if introduced.

## 8. Broker normalization boundary

| Transformation | Current location |
|---|---|
| Logical-to-broker symbol alias | `services.lebprime_live.symbols.resolve_symbol()` selects the configured connector symbol. |
| Digits/point and price normalization | `MT5Connector.get_symbol_spec()` and pending request construction. |
| Volume step/min/max normalization | `RiskManager` size/margin fit followed by connector request validation. |
| Stop-level/min-distance adjustment | `MT5Connector.build_pending_limit_request()` and its validation helpers. |
| Filling mode negotiation | `MT5Connector.choose_filling_mode()` below the gateway. |
| Margin calculation | MT5 connector calculation, consumed by risk margin fitting. |
| Pending order enum/time policy | Connector pending-order type and request builder. |

Strategy intent fields are never overwritten by these transformations; requested and broker-normalized values remain separate.

## 9. Replay/backtest compatibility

Shared today:

- `BrokerBase` submission/cancel/modify/close shape
- `ExecutionRequest`
- generic `ExecutionService`
- pending and execution policy concepts
- current simulated open positions and pending orders

Live-only capabilities:

- MT5 connection/symbol selection and terminal/account trade mode
- live tick/rate acquisition and broker symbol specification
- broker margin calculation, order check/send retcodes, filling modes
- authoritative MT5 order/deal/position history

Replay-only capabilities:

- deterministic clock and bar visibility
- bar-driven fills, expiration, and simulated account state
- playback frame/event recording

`ReplayBroker` should not implement `LebPrimeBrokerGateway` in Phase 4. It can implement smaller execution/state protocols if the live and replay orchestration converge later.

## 10. Telegram boundary

Telegram remains outside execution authority:

- It may display notifications and issue operator/control-plane commands.
- Notification failures are caught/logged and do not block broker placement.
- Telegram does not create strategy intent, approve risk, submit orders, or reconcile broker state.
- Trading continues according to configured runtime controls even when Telegram is unavailable.

## 11. Remaining direct concrete dependencies

Direct `MT5Connector` construction remains in:

- `main.py` for the process-wide connector and non-LebPrime generic lifecycle
- `MT5Broker` for generic/manual execution
- `ManualTradeService`
- `BacktestRunner` and Indicator Lab dataset loading for historical MT5 acquisition
- `LiveFeedService` and bootstrap diagnostics/scripts

These are outside the newly bounded LebPrime dependency and need separate adapters or an explicit decision that low-level data acquisition should remain concrete.

## 12. Phase 5 targets

- Decide whether the generic fallback candidate path belongs in the production process; only then can `TradingBot.strategy` be replaced by context-only construction.
- Reconcile `MT5Broker` and `MT5BrokerGateway` without changing the LebPrime request pipeline.
- Define a shared exposure/account snapshot if generic and LebPrime risk approval are to converge.
- Decide whether generic and LebPrime position lifecycle managers can share events without losing strategy-specific semantics.
- Add a typed LebPrime intent only as a migration of existing fields, with parity evidence.
- Migrate external users away from deprecated APIs and legacy config aliases before removal.
