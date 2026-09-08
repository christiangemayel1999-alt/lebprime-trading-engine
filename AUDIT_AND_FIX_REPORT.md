# Trading Bot Audit and Fix Report

Date: 2026-04-22

Scope: LIVE, dry/demo controls, BACKTEST, OOS, dashboard APIs/UI, worker lifecycle, MT5 data fetching, strategy engine selection, risk/exposure controls, trade lifecycle, journaling paths, and regression tests.

Validation status: full local test suite passed.

```powershell
& 'C:\Users\Administrator\AppData\Local\Python\bin\python.exe' -m pytest tests -q
# 154 passed, 3 warnings
```

## Executive Summary

The main regressions were caused by runtime drift: dashboard-selected strategy/mode settings were not always the same settings used by the executing engine, BACKTEST accepted execution-model values that the dashboard or schema rejected, V1/V2 engine selection could collapse onto the unified path, and several worker/OOS/DB edge cases made queued jobs or resumed jobs look healthy while not actually executing.

The fixes restore explicit V1/V2 engine routing, make strategy selection authoritative in backtests, align dashboard/schema/runner execution model contracts, harden MT5 and empty-data handling, add worker health and startup resilience, and close several live/backtest lifecycle gaps.

## Deep Issue Report

| Area | Issue | Root Cause | Fix |
| --- | --- | --- | --- |
| Strategy wiring | V1 could run through the unified engine. | `build_strategy_engine()` did not preserve the legacy V1 path. | V1 now builds `StrategyEngine`; V2 and other unified modes build `UnifiedStrategyEngine`; execution-mode summaries report truthful `engine_type`. |
| Backtest strategy selection | Dashboard strategy choices could be stale or ignored after engine construction. | Backtest built or refreshed strategy config in more than one place. | Backtest now reapplies selected strategy/family truth after engine creation and rejects explicit empty or unknown strategy selections. |
| Execution model contract | `current_bar_close` existed in runner paths but was rejected or missing in schemas/UI. | Execution model names were duplicated across layers. | Added canonical support in runner, dashboard schemas, OOS schemas, and UI dropdowns; fill semantics are explicit. |
| MT5 historical data | Invalid `copy_rates_range` params could crash backtests. | MT5 error `-2` / invalid params was treated as a fatal runtime error with no fallback. | Fetch range now normalizes UTC windows, validates ranges, falls back to count-based fetch where appropriate, filters to requested range, and returns clean no-data results. |
| Backtest endpoint no-data handling | Known no-data cases surfaced as 500s. | `_fetch_range_or_empty()` did not classify MT5 invalid params or empty fallback windows. | Known no-data/invalid-range cases return a standard empty OHLC dataframe with warnings; unexpected runtime errors still raise. |
| Worker lifecycle | Jobs could stay queued when dashboard was launched through bootstrap instead of the batch wrapper. | Worker startup lived in `start_dashboard.bat`, not the Python launch path. | Bootstrap and dashboard startup now ensure a worker unless disabled; duplicate starts are avoided; worker status is visible. |
| Worker observability | Dashboard could not tell queued-without-worker from a slow job. | No heartbeat/status contract. | Worker writes `storage/worker_status.json`; dashboard exposes `/api/worker/status` and renders running/stale/not-running state. |
| Request validation | Invalid date ranges and empty strategy lists could be queued. | Dashboard/API schemas were permissive and backend validation was incomplete. | Start/end dates are required and ordered; empty/unknown strategy selection is rejected before execution. |
| OOS workflow | Resume could be blocked by a stale active job even when the related run was terminal. | Job service checked active job status without reconciling related run state. | Resume reconciles terminal related runs, finalizes stale job state, then allows safe resume. |
| OOS dashboard fallback | Some tests and lightweight deployments had an OOS service but no job service. | Routes assumed job service always existed. | OOS run/resume routes queue through job service when present and fall back to direct OOS service manifest operations otherwise. |
| Database reliability | Windows test DB files stayed locked. | SQLite context manager committed but did not close connections. | Database connections now close deterministically via a context-managed `_connect()`. |
| Windows imports | Control state failed to import on Windows without `fcntl`. | POSIX-only lock import was unconditional. | `fcntl` is optional; Windows falls back to cooperative lock behavior. |
| Status endpoint | Minimal dashboard app contexts could fail `/api/status`. | Route assumed config manager implemented `source_of_truth_snapshot()`. | Endpoint now falls back to available config/runtime state. |
| Strategy empty-slice analysis | Market-context analysis could crash when `now_utc` was `None`. | Session classification used `None` without fallback. | Uses timezone-aware current UTC when no explicit timestamp is supplied. |
| Exposure policy | Dashboard multi-position toggle could still cap runtime to one position. | `allow_multi_position=true` did not override `max_concurrent_positions=1`. | Effective max becomes at least 2 when multi-position is enabled; reason-code ordering now reports same-family blocks before generic max-position blocks. |
| OOS V2 scenario | V2 full OOS matrix did not expose expected runtime overrides. | Scenario definition relied on inherited defaults. | V2 full scenario now declares multi-position and family-specific management overrides explicitly. |
| Hot reload classification | Risk-only dashboard patches could look restart-required. | Normalized `validation.*` and `shorts.*` defaults were not considered hot-reloadable. | Added those prefixes to hot-reloadable classification. |
| Trade lifecycle parity | Risk management evaluation mutated position state before executor action application. | Evaluator and executor both owned lifecycle mutation. | Evaluator now emits actions only; executor owns partial-close/breakeven state updates. |
| Test drift | Some tests inherited repository V1/V2 or strategy defaults accidentally. | Test configs were not explicit about intended mode/strategy. | Tests now pin execution mode and strategy activation where behavior depends on them. |

## Changed Files

Primary files changed in this remediation wave:

- `strategy_factory.py`
- `trading_bot/core/execution_mode.py`
- `services/backtest_runner.py`
- `mt5_connector.py`
- `dashboard/schemas.py`
- `dashboard/routes.py`
- `dashboard/services.py`
- `dashboard/static/dashboard.js`
- `services/worker.py`
- `services/worker_control.py`
- `services/job_service.py`
- `services/database.py`
- `services/config_manager.py`
- `services/control_state.py`
- `scripts/bootstrap_launcher.py`
- `scripts/run_dashboard.py`
- `scripts/run_worker.py`
- `risk_manager.py`
- `strategy.py`
- `trading_bot/execution/exposure_policy.py`
- `trading_bot/backtest/oos_evaluation.py`
- `lebprim_backtest_runner.py`
- `tests/test_comprehensive_audit_fixes.py`
- `tests/test_execution_mode_control.py`
- `tests/test_unified_architecture.py`
- `tests/test_mode_startup_separation.py`
- `tests/test_dashboard_status_api.py`
- `tests/test_job_worker_system.py`
- `tests/test_oos_dashboard_workflow.py`
- `tests/test_bot_v2_redesign.py`
- `tests/test_lebprim_execution.py`

## LIVE vs BACKTEST Parity Status After Fixes

- V1 and V2 now resolve through the same execution-mode patch path before engine construction.
- V1 uses the legacy `StrategyEngine`; V2 and unified modes use `UnifiedStrategyEngine`.
- Backtest strategy/family selection is applied to the actual engine used for simulation, not only to an earlier config copy.
- Backtest request validation now protects the same core assumptions live execution depends on: valid dates, valid strategy names, valid execution model, valid balance/risk inputs.
- Risk management evaluation no longer mutates trade lifecycle state before execution handling, reducing live/backtest divergence around partial close and breakeven.
- MT5 historical fetches now degrade to explicit no-data states instead of crashing the backtest path.

Remaining parity gap: live execution still depends on broker/MT5 runtime side effects that unit tests do not fully simulate. A live dry-run smoke test is still recommended before production trading.

## Dashboard Authority Status After Fixes

- Dashboard-selected execution model, strategy selection, execution mode, and worker availability are now reflected in backend validation and execution paths.
- Invalid dashboard backtest payloads are rejected before queueing.
- Queued jobs now include worker availability metadata/warnings where relevant.
- Dashboard startup and bootstrap paths ensure a worker is available unless disabled with `DISABLE_BACKTEST_WORKER=1` or `--no-worker`.
- `/api/worker/status` exposes running/stale/unavailable status, active job id, heartbeat age, and last-seen timestamps.
- `/api/status` is more robust in lightweight/minimal app contexts and reports effective runtime truth when available.

## Tests Added or Updated

- Strategy factory V1/V2 split and truthful `engine_type`.
- Dashboard execution-mode/status contracts.
- Backtest invalid date range, invalid balance, invalid risk, invalid execution model, empty strategy selection, and unknown strategy selection.
- Backtest `current_bar_close` execution model support.
- MT5 invalid-params/no-data fallback behavior.
- Worker heartbeat/status and no-duplicate startup behavior.
- OOS dashboard workflow start/resume behavior.
- Exposure policy V2 multi-position behavior.
- Trade lifecycle partial-close and breakeven regression coverage.
- LEBPRIM execution path tests pinned to intended strategy mode.

## Validation Checklist

Run automated validation:

```powershell
& 'C:\Users\Administrator\AppData\Local\Python\bin\python.exe' -m pytest tests/test_mode_startup_separation.py -q
& 'C:\Users\Administrator\AppData\Local\Python\bin\python.exe' -m pytest tests/test_execution_mode_control.py -q
& 'C:\Users\Administrator\AppData\Local\Python\bin\python.exe' -m pytest tests/test_lebprim_execution.py -q
& 'C:\Users\Administrator\AppData\Local\Python\bin\python.exe' -m pytest tests -q
```

Manual dashboard validation:

1. Start through `Start Bot.bat`; confirm dashboard starts and `/api/worker/status` reports a fresh running worker.
2. Queue a backtest with one selected strategy; confirm the run uses only that strategy.
3. Queue a backtest with no selected strategies; confirm a clear validation error.
4. Try `next_bar_open`, `signal_price_touch`, and `current_bar_close`; confirm each is accepted and reflected in the run.
5. Switch V1/V2 and confirm logs/status show `legacy_strategy_engine` for V1 and `unified_strategy_engine` for V2.
6. Submit an invalid date range; confirm it is rejected before queueing.
7. Run OOS start/resume from the dashboard and confirm the worker claims the queued job.
8. Temporarily stop the worker and queue a job; confirm the dashboard warning makes worker absence visible.

## Known Remaining Risks

- `dashboard/services.py` and `services/backtest_storage.py` still emit `datetime.utcnow()` deprecation warnings in tests.
- The Windows control-state fallback is cooperative when `fcntl` is unavailable; a true cross-platform kernel file lock would be stronger.
- The repo contains generated backtest artifacts/reports in the working tree; they were not removed because they may be user output.
- This pass used automated tests and code tracing, not a live broker order smoke test.
- Some config and trade payloads remain dictionary-heavy; typed DTOs would reduce future drift further.

## Recommended Next Optimization Wave

- Move shared enums such as execution model, execution mode, strategy family, event reason, and worker status into one typed contract module.
- Replace critical config dictionaries with Pydantic DTOs at API, runtime, and artifact boundaries.
- Add a broker-sim integration test that replays live order lifecycle events through the same journal/dashboard read paths.
- Add a true cross-platform file-lock helper, for example using `msvcrt` or a small dependency such as `portalocker`.
- Convert remaining UTC timestamp code to timezone-aware `datetime.now(timezone.utc)`.
- Separate generated artifacts from source-controlled paths and enforce cleanup/ignore rules.
