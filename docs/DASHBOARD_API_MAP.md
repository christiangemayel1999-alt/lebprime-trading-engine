# Dashboard API Map

This map describes the production dashboard after Phase 1. The live execution path remains owned by `LebPrimeLiveService`; dashboard lifecycle endpoints are read-only.

## Surviving sections

| Dashboard section | Frontend loader / renderer | HTTP API | Route handler | Service / repository | Primary source of truth |
|---|---|---|---|---|---|
| Overview | `refreshStatus()`, `refreshLebprimeReadModels()`, `fetchWorkerStatus()` -> `renderOverview()` | `GET /api/status`, `GET /api/lebprime/status`, `GET /api/trades/lebprime`, `GET /api/worker/status` | `get_status`, `get_lebprime_status`, `get_lebprime_trades`, `get_worker_status` | control state, `DashboardDataService`, `LebPrimeLiveService`, `LebPrimeLiveRepository`, worker status service | persisted runtime control state, LebPrime runtime/context rows, `lebprime_broker_entries`, worker heartbeat |
| LebPrime | `refreshConfig()`, `refreshStatus()` -> `renderStrategies()` | `GET /api/config`, `POST /api/config/preview`, `POST /api/config`, `GET /api/lebprime/status` | `get_config`, `preview_config`, `update_config`, `get_lebprime_status` | control plane/config manager, `LebPrimeLiveService`, `LebPrimeLiveRepository` | canonical config (`lebprime_v3_4_0`) plus persisted/runtime LebPrime context, spread, zone, and broker-entry state |
| Trades | `refreshLebprimeReadModels()` -> `renderTrades()` | `GET /api/trades/lebprime` | `get_lebprime_trades` | `LebPrimeLiveService.dashboard_trade_snapshot()` -> `LebPrimeLiveRepository.dashboard_trade_snapshot()` | `lebprime_broker_entries` joined read-only to `lebprime_live_zones`; MT5-originated tickets/statuses persisted by broker reconciliation |
| Performance | `refreshLebprimeReadModels()` -> `renderPerformance()` | `GET /api/performance/lebprime` | `get_lebprime_performance` | `LebPrimeLiveService.dashboard_performance_snapshot()` -> `LebPrimeLiveRepository.dashboard_performance_snapshot()` | aggregate counts from `lebprime_broker_entries` and `lebprime_live_zones`, excluding historical replay entries |
| Risk | `refreshConfig()` -> `renderRisk()` | `GET /api/config`, `POST /api/config/preview`, `POST /api/config` | `get_config`, `preview_config`, `update_config` | control plane/config manager; live consumers include `LebPrimeLiveService`, risk manager, and position lifecycle | canonical `risk` and `exit` config sections |
| Research / Backtest | `fetchBacktests()`, `fetchJobs()` -> `renderBacktest()` | `GET /api/backtest/runs`, `GET /api/backtest/run/{run_id}`, `POST /api/jobs/backtest`, `GET /api/jobs` | corresponding backtest/job handlers in `dashboard/routes.py` | `DashboardDataService`, backtest storage, job service | persisted backtest runs, job records, result bundles |
| Research / Replay | `fetchBacktests()`, replay fetch/poll functions -> `renderBacktestReplay()` | `GET /api/backtests/{run_id}/replay/*`, `GET /api/backtests/{run_id}/stream/*` | replay/stream handlers | `DashboardDataService` | backtest playback frames/events and run stream state |
| Research / Indicator Lab | `fetchIndicatorLab()` -> `renderIndicatorLab()` | `/api/indicator-lab/datasets*`, `/api/indicator-lab/parameters/*`, `/api/indicator-lab/campaigns*`, `/api/indicator-lab/tests/*`, `/api/indicator-lab/zones/*` | Indicator Lab handlers | `IndicatorLabService` | Indicator Lab datasets, campaign/test results, zones, and events |
| Research / OOS | `fetchOOS()` -> `renderOOS()` | `GET /api/oos/status`, `GET /api/oos/history`, `POST /api/oos/run`, `POST /api/oos/resume`, `POST /api/oos/rebuild-report` | OOS handlers | `OOSService`, job service | persisted OOS runs/scenarios and generated reports |
| Config | `refreshConfig()`, `refreshLogs()` -> `renderConfig()`, `renderAlerts()` | `GET/POST /api/config`, `POST /api/config/preview`, `GET /api/config/export`, `POST /api/config/import`, `POST /api/config/reload`, `POST /api/config/clear-pending-restart`, `GET /api/audit` | config/audit handlers | control plane, config manager, audit service | canonical configuration and audit log; Telegram stays notification/remote-control configuration and is not an execution trigger |
| Logs | `refreshLogs()` -> `renderLogs()` | `GET /api/logs`, `GET /api/audit` | `get_logs`, `get_audit` | `DashboardDataService`, audit service | persisted bot events/logs and audit records |
| System Health | `refreshStatus()`, `fetchWorkerStatus()`, `fetchJobs()` -> `renderHealth()` | `GET /api/status`, `GET /api/worker/status`, `GET /api/jobs` | status/worker/job handlers | control state, `DashboardDataService`, job/worker services | runtime/control heartbeat, worker heartbeat, persisted jobs |

## LebPrime trade read model

`GET /api/trades/lebprime` returns:

- `pending`: `PLACING`, `RETRY_SCHEDULED`, `PENDING`, and unresolved `ORDER_HISTORY` entries.
- `open`: broker-confirmed `FILLED` entries.
- `closed`: `CLOSED` entries.
- `failed`: placement failures, rejections, risk/precheck blocks, cancellations, and expirations.
- `summary`: category counts, cancellation/expiration counts, the number of excluded historical replay rows, and explicit PnL availability.

The normalized rows expose persisted symbol, timeframe, zone/entry identity, side/type, requested and normalized entry, volume, zone strategy SL, submitted broker SL, TP1/TP2, order/deal/position tickets, timestamps, status, failure reason, and retry count. `close_price` and `realized_pnl` are intentionally `null` because those values are not stored on `lebprime_broker_entries` today.

Historical rows (`historical = 1`) are excluded so replay state cannot be presented as executable/live activity.

## Configuration classification

- Live production: `lebprime_v3_4_0`, relevant shared `risk`, MT5 connection/execution guard settings, runtime control state, dashboard operations settings, and Telegram notification settings.
- Research only: backtest, replay, Indicator Lab, and OOS configuration/data.
- Legacy compatibility: generic `strategy.setup_controls`, `strategy.setup_families`, generic session controls, and generic symbol controls. These remain in configuration because compatibility/backtest code may read them, but they are not exposed as active LebPrime production controls.

## Deprecated or unused endpoint candidates

These remain available for compatibility and are not deleted in Phase 1:

- `GET /api/strategies/lebprime-v3-4-0/status`: replaced in the dashboard by `GET /api/lebprime/status`.
- `GET /api/strategies`, `POST|PATCH /api/strategies/settings`, `POST /api/strategies/toggle`, and `POST /api/families/toggle`: no longer used by the production dashboard.
- `GET|POST|PATCH /api/sessions*` and `/api/symbols*`: generic controls no longer used by the production dashboard; LebPrime symbols/timeframes live under `lebprime_v3_4_0`.
- `/api/manual/*` and `POST /api/control/close-all`: intentionally preserved backend operations, but Manual Trading is absent from navigation and JavaScript.
- `GET /api/backtests`: overlaps with `GET /api/backtest/runs`; research currently uses the latter.
- `GET /api/risk` and `POST|PATCH /api/risk/update`: current Risk UI uses the canonical config preview/update flow; consolidate only after all non-dashboard callers are checked.
- `GET /api/execution` and `POST|PATCH /api/execution/update`: not called by the surviving dashboard UI.

## Known source limitations

- LebPrime broker entries do not persist exact close price or realized PnL, so win rate, TP/SL outcome, and PnL are not calculated.
- Current broker reconciliation collapses several terminal MT5 order-history states into `CANCELLED`; exact expired-versus-rejected attribution cannot be reconstructed from persisted rows.
- `/api/status` still includes generic trade/performance aggregates for compatibility. The surviving Trades and Performance sections do not consume those fields.
