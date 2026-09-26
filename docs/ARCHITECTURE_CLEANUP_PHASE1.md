# Architecture Cleanup Phase 1

## 1. Files changed

- `dashboard/templates/index.html`
- `dashboard/static/dashboard.js`
- `dashboard/routes.py`
- `services/lebprime_live/service.py`
- `services/lebprime_live/repository.py`
- `docs/DASHBOARD_API_MAP.md`
- `docs/ARCHITECTURE_CLEANUP_PHASE1.md`

No signal mathematics, Pine logic, order placement behavior, SL/TP behavior, risk sizing, broker reconciliation, Telegram independence, demo guard, or persisted trading record was changed.

## 2. Dashboard sections merged

- Strategies, Symbols, and relevant runtime context are consolidated into **LebPrime**. The visible controls are the actual `lebprime_v3_4_0` symbol/timeframe and operational settings.
- Backtest, Backtest Replay, Indicator Lab, and OOS are consolidated as internal tabs under **Research**.
- Alerts/Telegram settings are consolidated under **Config**.
- Trades now represents the persisted LebPrime broker lifecycle rather than the generic trade-recovery model.
- Performance now represents persisted LebPrime zone/order lifecycle counts rather than the disconnected generic trade model.

## 3. Sections removed from top-level navigation

- Strategies
- Sessions
- Symbols
- Manual Trading
- Alerts
- Backtest
- Backtest Replay
- Indicator Lab
- OOS

The resulting navigation is Overview, LebPrime, Trades, Performance, Risk, Research, Config, Logs, and System Health.

## 4. APIs added

- `GET /api/lebprime/status`: canonical production-strategy status alias.
- `GET /api/trades/lebprime`: normalized, read-only live broker lifecycle grouped into pending/open/closed/failed categories.
- `GET /api/performance/lebprime`: read-only lifecycle counts and symbol/timeframe/entry/side/status breakdowns.

## 5. APIs reused

- Runtime/control: `/api/status`, `/api/worker/status`, `/api/control/*`, `/api/runtime/*`.
- Configuration: `/api/config`, `/api/config/preview`, import/export/reload/restart-marker endpoints.
- Research: existing backtest, replay, stream, Indicator Lab, OOS, and job endpoints.
- Operations: `/api/logs`, `/api/audit`, `/api/jobs`.

## 6. APIs deprecated but not deleted

- Legacy LebPrime status path `/api/strategies/lebprime-v3-4-0/status`.
- Generic strategy/family settings and toggle endpoints.
- Generic sessions and symbols endpoint families.
- Manual-trading endpoints and close-all control endpoint.
- Duplicate backtest listing endpoint `/api/backtests`.
- Standalone risk/execution update families currently bypassed by the canonical config update UI.

See `docs/DASHBOARD_API_MAP.md` for the detailed caller/source map.

## 7. Legacy strategy controls hidden

The production UI no longer renders generic controls for `trend_pullback_reclaim`, `breakout_retest_continuation`, `liquidity_sweep_reversal`, `compression_release`, or `lebprim_scalp`. Those configuration keys and implementations remain untouched for compatibility and research. LebPrime v3.4.0 is explicitly labeled as the active production strategy.

Generic session and symbol controls were also hidden because `LebPrimeLiveService` selects its live symbols and timeframes from `lebprime_v3_4_0`; presenting generic controls as live LebPrime controls would be misleading.

## 8. Backend components deliberately preserved

- `services/lebprime_live/`
- `services/indicator_lab/`
- `trading_bot/execution/`
- `trading_bot/risk/`
- `trading_bot/positions/`
- `mt5_connector.py`, `risk_manager.py`
- database, trade journal, config manager, Telegram, worker, and backtest services
- `ManualTradeService` and all manual backend routes
- compatibility strategy factory and disabled generic strategy implementations

The dashboard additions only read existing LebPrime tables. Existing writes remain inside live services.

## 9. Unresolved architecture duplication

- `/api/status` still assembles generic `trades`-table metrics alongside operational state. It remains for compatibility and health data, while Trades/Performance now use LebPrime endpoints.
- The old and canonical LebPrime status routes currently coexist.
- Generic config section routes coexist with the canonical config patch flow.
- Two backtest list endpoints coexist.
- Exact closed-entry price and realized PnL are absent from `lebprime_broker_entries`.
- Persisted terminal broker status does not reliably distinguish canceled, expired, and rejected history states because current reconciliation normalizes several states to `CANCELLED`.

## 10. Recommended Phase 2 deletions

Only after repository-wide caller checks and targeted validation:

1. Remove the old LebPrime status route after external callers migrate.
2. Remove unused generic strategy/family/session/symbol dashboard routes and their request schemas if no external clients use them.
3. Retire `/api/backtests` in favor of `/api/backtest/runs`.
4. Consolidate risk/execution section routes with the canonical config update path.
5. Decide whether manual-trading endpoints belong in a separate operator tool before deleting `ManualTradeService`.
6. Split research code from `dashboard.js` into focused static modules once the current navigation/data contract is stable.
7. Add an authoritative persisted broker-outcome projection (including close price and realized PnL) sourced from MT5 reconciliation/trade journal, then extend Performance without candle-based inference.
8. Remove generic trade/performance fields from `/api/status` only after all compatibility consumers have migrated.

## Verification boundary

Per task instructions, no pytest, unit, integration, frontend, smoke, or full test suite was run. Verification was limited to Python compile checks, JavaScript syntax checking, import/search inspection, and `git diff --check`.
