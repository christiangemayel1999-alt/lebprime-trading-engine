# Architecture Cleanup Phase 2

## Scope and safety boundary

Phase 2 completes the persisted LebPrime broker lifecycle and updates the read-only Trades and Performance projections. It does not change signal generation, Pine/zone logic, Entry1/Entry2 rules, SL/TP math, risk percentage, lot sizing, pending-order semantics, the demo guard, Telegram independence, or historical/replay execution safety. No broker orders or fabricated zones were created.

## 1. Lifecycle fields now persisted

The existing `lebprime_broker_entries` table remains the single LebPrime entry write model. Additive, nullable migrations preserve existing `bot.db` rows and add:

- `fill_price`, `close_price`, `realized_pnl`, and `partial_realized_pnl`
- `broker_profit`, `broker_commission`, `broker_swap`, and `broker_fee`
- `final_exit_reason`, `broker_retcode`, and `broker_submitted_at`
- `canceled_at` and `expired_at`
- `filled_volume`, `closed_volume`, `remaining_volume`, and `partial_close_count`
- `outcome_complete` and `position_model`
- `close_deal_tickets_json` and `lifecycle_details_json`

Existing columns continue to carry the order/deal/position tickets, broker status, created/filled/closed timestamps, failure reason, retry state, and requested broker values. The API aliases these to the canonical field names requested by the dashboard contract.

## 2. Broker reconciliation logic

`LebPrimeLiveService._reconcile_broker_execution()` obtains current pending orders, bot positions, bot order history, and bot deal history from the existing connector wrappers. Once a position ticket is known it also requests the complete position-deal history, which can include broker/manual closing deals that do not carry the bot magic number.

The reconciliation order is:

1. A matching current broker order remains `PENDING`.
2. A matching entry deal or position ticket records the exact fill ticket, time, price, volume, and position model.
3. A currently open position is `FILLED`; newly observed closing deals update partial-close volume/PnL metadata.
4. A position absent from the current position set is closed only when closing deal history exists.
5. A never-filled order absent from the current set is classified from its final order-history state.

No candle touch or price-proximity inference is used for fills, closes, or exit outcomes.

## 3. Close and PnL source of truth

For attributable hedging positions, the final close time and volume-weighted close price come from closing MT5 deals. Realized PnL is the sum of `profit + commission + swap + fee` over all position deals, preserving each component separately. Close-deal tickets and compact deal snapshots are retained so partial closes do not overwrite earlier history.

## 4. Exit-reason classification

Closing deals are normalized to `TP`, `SL`, `MANUAL_CLOSE`, `BROKER_CLOSE`, or `UNKNOWN` from MT5 deal reason and broker comment data. Recognized LebPrime TP/SL comments are considered broker-history evidence. Reconciliation does not guess from distance to TP or SL.

## 5. Canceled, expired, and rejected classification

Accepted pending orders that disappear are checked in MT5 order history. Final MT5 order states are persisted separately as `CANCELED`, `EXPIRED`, or `REJECTED`, with terminal timestamps and normalized exit reasons. Immediate broker-send rejection records the retcode and rejection reason. Pre-submit/order-check failures keep their existing failure statuses and are not rewritten as accepted broker-order terminal outcomes.

## 6. Restart reconciliation behavior

Reconciliation rebuilds pending and open state from current broker data and fills missing lifecycle fields from order/deal history. Historical/replay rows remain excluded. Existing placement idempotency keys are unchanged, so reconciliation cannot place a second order. Journal event keys combine the LebPrime entry identity, transition, and close-deal ticket where applicable; the existing journal uniqueness constraint suppresses duplicates across cycles and restarts. Reconciliation does not emit CREATED alerts.

Closed incomplete rows with an unknown model can be enriched on a later cycle if broker history becomes available. Rows already proven to be netting-attribution cases are not repeatedly queried because exact per-entry recovery is not possible from shared position PnL.

## 7. APIs removed

None. Repository tracing found no safe endpoint deletion that passed every requested gate. In particular, the duplicate `GET /api/backtests` list route has no dashboard caller but remains documented in `PROJECT_CONTEXT.md`, so it was retained.

## 8. APIs deprecated

The following compatibility routes are marked deprecated in FastAPI/OpenAPI while preserving their behavior:

- `GET /api/strategies/lebprime-v3-4-0/status`
- generic strategy list/settings/toggle and family-toggle routes
- generic risk and execution section routes
- generic session and symbol section routes
- duplicate `GET /api/backtests`

`/api/status` remains unchanged. Its operational fields are required, and its generic trade data still supplies Logs compatibility fields such as blocked and valid-not-executed setups.

## 9. Backend modules preserved

`strategy.py`, `strategy_factory.py`, `lebprim_strategy.py`, `filters.py`, `indicators.py`, and the entire `trading_bot/strategy/` package remain. Current imports show the main live runtime uses `strategy_factory` for runtime support and `trading_bot.strategy` diagnostics/shadow support; backtest and OOS paths also depend on this graph. Manual operations, legacy dashboard services, replay endpoints, worker services, Telegram, database, and trade-journal components are also preserved.

## 10. Phase 3 deletion candidates

| Classification | Component | Evidence / next gate |
|---|---|---|
| SAFE TO REMOVE | None yet | No candidate passed all external-documentation and runtime-caller gates. |
| LIKELY REMOVE | Duplicate `GET /api/backtests` list route | No surviving dashboard caller; migrate/remove its `PROJECT_CONTEXT.md` contract and confirm external clients first. |
| LIKELY REMOVE | Generic strategy/family/session/symbol/risk/execution dashboard routes | Hidden from the Phase 1 UI; confirm external operator clients and tests that encode real compatibility needs. |
| LIKELY REMOVE | Unused generic trade/performance fields in `/api/status` | Trades/Performance use LebPrime APIs; split fields only after mapping Logs, Health, and external status consumers field by field. |
| KEEP | `strategy.py`, `strategy_factory.py`, `indicators.py`, `trading_bot/strategy/` | Imported by live runtime support and/or active research/backtest paths. |
| KEEP | Legacy dashboard services as a whole | Still assemble status, Logs, Health, research, jobs, and operational data. |
| KEEP | Replay `/api/backtests/{run_id}/...` APIs and `/api/backtest/runs` | Called by the surviving Research UI. |
| UNKNOWN / NEEDS MORE TRACE | `lebprim_strategy.py`, `filters.py` | Reachable through strategy adapters/research imports; require mode-by-mode runtime coverage before removal or relocation. |

## 11. Unresolved attribution issue

MT5 netting accounts can combine Entry1, Entry2, and other activity in a shared symbol position. Assigning the shared position result to each entry would double count PnL. Phase 2 therefore records the detected model but leaves per-entry close price, PnL, and exit reason incomplete for netting/unknown attribution. A future solution needs a deterministic deal-allocation ledger, not heuristic splitting.

## 12. Incomplete historical data limitation

Older rows may lack position tickets, close-deal tickets, or broker history retained by MT5. Such rows remain readable and are reported as incomplete; they are never silently included in win rate or PnL metrics. Average R remains unavailable because a reliable durable per-entry risk basis is not currently persisted.

## Dashboard and journal result

Trades now distinguishes pending, open, closed, canceled, expired, rejected, and precheck-failed states while rendering unknown values as an em dash. Performance labels outcome arithmetic as complete-record-only and provides symbol, timeframe, and entry-number breakdowns. Lifecycle transitions use the existing trade journal for `ORDER_ACCEPTED`, `ORDER_FILLED`, `POSITION_OPENED`, `POSITION_PARTIAL_CLOSE`, `POSITION_CLOSED`, `ORDER_CANCELED`, `ORDER_EXPIRED`, and `ORDER_REJECTED` with deterministic event keys.

## Verification boundary

Per the Phase 2 request, no pytest, integration, frontend, smoke, or full automated suite was run. Verification is limited to Python compilation of edited modules, JavaScript syntax checking, repository caller/import tracing, and `git diff --check`. This is static validation, not full runtime or broker validation.
