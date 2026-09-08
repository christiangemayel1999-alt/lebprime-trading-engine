# Codebase Cleanup Report

Date: 2026-09-08

## Current Architecture

The project currently contains both legacy/core modules at the repository root and a newer package under `trading_bot/`. The newer package is being introduced through adapters rather than a full rewrite, which is appropriate for a trading system that needs behavior preservation.

Live startup flows through `main.py`. It loads runtime configuration, initializes `DatabaseService`, `ControlStateService`, `MT5Connector`, `TelegramCommandService`, `TelegramNotifier`, `ExecutionService(MT5Broker(...))`, and the strategy engine returned by `strategy_factory.build_strategy_engine`.

Backtesting flows through `services/backtest_runner.py` and `trading_bot/backtest/runner.py`. `BacktestRunner` loads historical candles through `HistoricalDataLoader`, builds a mode-aware strategy engine, applies risk/exposure/pending-order checks, creates canonical execution requests, and sends them through `ExecutionService(ReplayBroker(SimulatedExecutor(...)))`.

The intended shared execution direction is present:

- LIVE: Strategy -> Risk -> Execution Decision -> Canonical Execution Plan -> ExecutionService -> MT5Broker -> MT5Connector.
- BACKTEST: Strategy -> Risk -> Execution Decision -> Canonical Execution Plan -> ExecutionService -> ReplayBroker -> SimulatedExecutor.

## Important Entry Points

- Live bot startup: `main.py`, `Start Bot.bat`, `scripts/start_bot.bat`.
- Dashboard startup: `dashboard/app.py`, `scripts/run_dashboard.py`, `scripts/start_dashboard.bat`, `Start Dashboard.bat`.
- Strategy factory: `strategy_factory.py`.
- Strategy registry/adapters: `trading_bot/strategy/registry.py`, `trading_bot/strategy/adapters.py`, `trading_bot/strategy/engine.py`.
- Legacy strategies: `strategy.py`, `lebprim_strategy.py`.
- Risk path: `risk_manager.py`, `trading_bot/risk/engine.py`, `trading_bot/risk/position_sizing.py`, `trading_bot/risk/management_profiles.py`.
- Execution path: `trading_bot/execution/execution_service.py`, `trading_bot/execution/decision_engine.py`, `trading_bot/execution/canonical.py`.
- MT5 broker: `trading_bot/execution/mt5_broker.py`.
- Replay broker: `trading_bot/execution/replay_broker.py`.
- Simulated executor: `services/simulated_executor.py`.
- Backtesting: `services/backtest_runner.py`, `trading_bot/backtest/runner.py`, `trading_bot/backtest/oos_evaluation.py`.
- Database: `services/database.py`.
- Control plane: `services/control_plane_service.py`, `services/control_state.py`, `services/operator_service.py`, dashboard routes, Telegram command service.
- Telegram integration: `services/telegram_notifier.py`, `services/telegram_command_service.py`.

## Project Inventory

- Python files: 124.
- Test files: 21.
- Service Python files: 27.
- `trading_bot/` Python files: 50.
- Dashboard files: 12.
- Config/docs/scripts present: `config.json`, `.env.example`, `.gitignore`, `README.md`, `PROJECT_CONTEXT.md`, `AUDIT_AND_FIX_REPORT.md`, `FULL_PATCH_EXPORT.md`, root batch launchers, `scripts/`.
- MT5 integration present in `mt5_connector.py`, `trading_bot/execution/mt5_broker.py`, `services/live_feed.py`, `services/manual_trade_service.py`.
- Telegram integration present in `services/telegram_notifier.py`, `services/telegram_command_service.py`, `main.py`, `dashboard/app.py`.

## Cleanup Performed

- Expanded `.gitignore` to exclude local secrets, caches, logs, databases, runtime state, generated backtests, generated reports, smoke-test folders, temp folders, and ZIP exports.
- Updated `.env.example` placeholders so credential-like values are clearly non-real.
- Made `mt5_connector.py` import-safe when the `MetaTrader5` Python package is not installed.
- Routed `trading_bot/execution/mt5_broker.py` and `services/manual_trade_service.py` through the connector's import-safe `mt5` boundary.
- Added this cleanup report.

## Generated / Runtime Artifacts

These should remain outside a future clean repository unless a specific artifact is intentionally promoted as documentation or fixture data:

- Python caches: `__pycache__/`, nested `__pycache__/`, `*.pyc`.
- Pytest caches/temp roots: `.pytest_cache*`, `.pytest-basetemp*`, `.pytest_basetemp*`, `.pytest_tmp*`, `pytest-cache-files-*`, `pytest_tmp/`.
- Runtime logs: `logs/`, nested `logs/`, `*.log`.
- Runtime DBs: `storage/bot.db`, smoke/test `bot.db`, `loader.db`, `runner.db`, `test_jobs.db`, `*.db-wal`, `*.db-shm`, `*.sqlite`, `*.sqlite3`.
- Runtime state: `state.json`, `storage/`, `storage/config_backups/`, control-state files under storage.
- Generated backtests/exports: `backtests/`, `backtest_results/`, generated run ZIPs such as `backtests/run_*.zip`.
- Generated reports: `reports/`, OOS evaluation folders such as `reports/oos_eval_*`, daily report CSV/MD outputs, setup/close/blocked reason CSVs.
- Smoke/temp folders: `loader_smoke_tmp/`, `playback_smoke_tmp/`, `playback_smoke_modes/`, `tmp*/`.
- Export bundles: `*.zip`.

No historical backtest data was deleted.

## Security Check

Potential secret-bearing local configuration exists in `.env`. Keys present include `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_PATH`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `DASHBOARD_PORT`. Values were not printed and `.env` remains ignored.

Source/config/documentation references to credential-shaped names were found in expected places: `.env.example`, `README.md`, `PROJECT_CONTEXT.md`, `config.json`, `main.py`, `mt5_connector.py`, `utils.py`, dashboard auth/static code, Telegram services, config manager, control-state/database services, and tests. The scan did not require exposing secret values.

`.env.example` now uses placeholder values only.

## MT5 Coupling

Before cleanup, importing `mt5_connector.py` required `MetaTrader5` immediately because the module imported `MetaTrader5` and read timeframe constants at class definition time. This blocked unrelated backtest/unit test collection on machines without the package.

`mt5_connector.py` now catches missing `MetaTrader5` at import time and exposes an import-safe placeholder with the constants needed for module/class construction and monkeypatched tests. Any actual MT5 API call raises a clear runtime error explaining that the `MetaTrader5` package and supported Windows MT5 environment are required.

Live Windows behavior is unchanged when `MetaTrader5` is installed: the real module is imported and used as before.

## Architecture Divergence

Known LIVE/BACKTEST divergence to preserve and test before changing:

- `main.py` still owns a large amount of live orchestration, runtime reload behavior, alerting, position management, and entry handling.
- `services/backtest_runner.py` reimplements a substantial orchestration loop for replay and accounting, even though it now uses shared execution service and canonical execution requests.
- Live lifecycle management is split between `main.py`, `trading_bot/positions/live_lifecycle.py`, and connector calls; replay lifecycle is mostly in `SimulatedExecutor` plus `BacktestRunner`.
- Risk has both legacy `risk_manager.py` and newer `trading_bot/risk/*` paths.
- Strategy selection uses `strategy_factory.py` to bridge legacy and unified engines, but legacy setup generation still lives in root strategy modules.

These are candidates for future parity work, not cleanup-time rewrites.

## Technical Debt

P0 - safety/correctness:

- Full suite runtime is too long/noisy to use as a quick safety gate in this environment.
- `.env` contains live-shaped credentials locally; this is expected for runtime but must never be committed.

P1 - blocks deterministic backtesting:

- `BacktestRunner.run` is monolithic and mixes data loading, strategy evaluation, risk decisions, execution, accounting, event persistence, and report generation.
- Replay parity tests are very log-heavy and slow; deterministic backtesting work will need faster, smaller scenario fixtures.
- Live and replay lifecycle/accounting still diverge across separate modules.

P2 - architecture/maintainability:

- `main.py`, `services/database.py`, `mt5_connector.py`, and strategy modules are oversized.
- Root legacy modules and `trading_bot/` modules coexist with adapter boundaries that need stronger contracts.
- `FULL_PATCH_EXPORT.md` appears to be a generated patch/archive document and should not become a future source-of-truth file.

P3 - cosmetic:

- Some docstrings/comments and import organization can be cleaned gradually after behavioral tests are stronger.
- Historical cache/output directories make the working tree hard to scan.

## Large Refactor Candidates

- `main.py` (4469 lines): split runtime bootstrap, config reload, live cycle orchestration, entry attempt, lifecycle management, alerting, and command/control handling after tests lock behavior.
- `services/backtest_runner.py` (2242 lines): extract data-window resolution, replay loop, canonical plan building, execution/accounting, event writing, and summary/report generation.
- `services/database.py` (3170 lines): separate schema/migrations, trade journal persistence, backtest storage, dashboard queries, and job/worker persistence.
- `mt5_connector.py` (2077 lines): split connection/session, symbol metadata, historical data, order request building, order send/check stages, position/history operations, and diagnostics.
- `strategy.py` / `lebprim_strategy.py`: isolate indicator preparation, candidate generation, setup scoring, session/regime gates, and management rules behind explicit contracts.
- `risk_manager.py`: separate legacy live risk state, risk calculations, duplicate/cooldown/exposure guards, and persistence synchronization.

## Test Results

- Import smoke: `mt5_connector`, `services.backtest_runner`, `trading_bot.execution.mt5_broker`, and `services.manual_trade_service` import successfully without a live MT5 connection.
- `python -m compileall .`: passed with exit code 0.
- `python -m pytest --collect-only -q`: 189 tests collected in 5.58s.
- `python -m pytest -q`: started successfully and produced initial passes, but was manually interrupted after several minutes without a final pytest summary. No collection/import-time MT5 failure occurred.
- `python -m pytest -q tests/test_mt5_connector_fallback.py`: 2 passed, 1 warning.
- `python -m pytest -q tests/test_trade_lifecycle_canonical.py`: 8 passed, 1 warning.
- `python -m pytest -q tests/test_execution_flow.py tests/test_unified_architecture.py`: 26 passed, 1 warning.
- `python -m pytest -q tests/test_backtest_live_like_parity.py::test_live_backtest_shared_execution_plan_parity`: 1 passed, 1 warning.
- `python -m pytest -q tests/test_lebprim_execution.py::test_mt5_fetch_rates_range_uses_count_fallback_and_filters tests/test_lebprim_execution.py::test_backtest_fetch_range_returns_empty_on_mt5_invalid_params`: 2 passed, 1 warning.
- `python -m pytest -q tests/test_backtest_replay_foundation.py::test_history_loader_falls_back_from_mt5_failure_to_csv tests/test_backtest_replay_foundation.py::test_history_loader_prefers_mt5_then_cache_then_csv tests/test_backtest_replay_foundation.py::test_history_loader_uses_csv_when_mt5_and_cache_fail`: 3 passed, 1 warning.
- Completed targeted validation total: 42 passed, 0 failed, 0 skipped, 0 errors.

The replay-heavy parity file was also attempted with verbose output. It was interrupted during `tests/test_backtest_live_like_parity.py::test_no_future_candles_in_signal_generation` because it was processing minute-by-minute playback with very high log volume and had not completed quickly enough for this cleanup pass.

## Trading Behavior

Intentional trading behavior changes: NONE
