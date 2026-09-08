# Repository Pruning Report

Date: 2026-09-08

Scope: final repository pruning after the cleanup and deterministic-baseline phases. No production trading logic was edited in this pass.

## Summary

Removed generated runtime state, test caches, smoke outputs, local databases, logs, backtest exports, report exports, bytecode caches, and one generated full-code export. Preserved source code, tests, deterministic fixtures, and useful project documentation.

## Deleted Directories

Root-level generated/runtime directories deleted:

- `.pytest-basetemp`
- `.pytest-basetemp-livefix`
- `.pytest-basetemp-local`
- `.pytest_basetemp_local`
- `.pytest_cache`
- `.pytest_cache_finalfix_targeted`
- `.pytest_cache_pnlfix_targeted`
- `.pytest_cache_pnlfix_targeted2`
- `.pytest_cache_task_livefix_escalated`
- `.pytest_task_livefix_escalated`
- `.pytest_tmp_finalfix_targeted`
- `.pytest_tmp_livefix_recovery`
- `.pytest_tmp_livefix_reports`
- `.pytest_tmp_local`
- `.pytest_tmp_pnlfix_targeted`
- `.pytest_tmp_pnlfix_targeted2`
- `.pytest_tmp_probe`
- `.pytest_tmp_probe2`
- `.pytest_tmp_run_92a1db1dc10143848ec86fdc3dbf58c2`
- `backtests`
- `loader_smoke_tmp`
- `logs`
- `playback_smoke_modes`
- `playback_smoke_tmp`
- `pytest-cache-files-_a3zpu77`
- `pytest-cache-files-2envzhfu`
- `pytest-cache-files-4i6fxt5n`
- `pytest-cache-files-4uct1vmj`
- `pytest-cache-files-51_9up3m`
- `pytest-cache-files-753xjjjg`
- `pytest-cache-files-8koyex_g`
- `pytest-cache-files-9wibirlk`
- `pytest-cache-files-a5fu931w`
- `pytest-cache-files-ab2xcl9_`
- `pytest-cache-files-b1_f0fug`
- `pytest-cache-files-cphsian8`
- `pytest-cache-files-d5z3vq3k`
- `pytest-cache-files-faxjh4pf`
- `pytest-cache-files-fb76shk9`
- `pytest-cache-files-fhgxxm1d`
- `pytest-cache-files-g83d4o2u`
- `pytest-cache-files-k2n519p_`
- `pytest-cache-files-kj3cg6st`
- `pytest-cache-files-l6lephjv`
- `pytest-cache-files-lqx56n4e`
- `pytest-cache-files-oqkwqp79`
- `pytest-cache-files-qcj652wj`
- `pytest-cache-files-r5mgkpwo`
- `pytest-cache-files-rgxmnn4k`
- `pytest-cache-files-rnjo9zqz`
- `pytest-cache-files-sb4v36ir`
- `pytest-cache-files-tx05cx31`
- `pytest-cache-files-ugbav48q`
- `pytest-cache-files-v9gx7kdr`
- `pytest-cache-files-wjivbrlw`
- `pytest-cache-files-xgtp61ft`
- `pytest-cache-files-yf8xstfy`
- `pytest-cache-files-zv12n34_`
- `pytest-cache-files-zx715wvh`
- `pytest-cache-files-zzikblfk`
- `pytest_tmp`
- `reports`
- `storage`
- `tmp_c3wwec8`
- `tmp_control_debug`
- `tmp_live_debug`
- `tmp_ps_temp`
- `tmp_smoke_replay`
- `tmp_test`
- `tmpj8dj22a0`
- `tmpxswe1mq1`
- `__pycache__`

Nested bytecode cache directories deleted:

- `dashboard/__pycache__`
- `scripts/__pycache__`
- `services/__pycache__`
- `tests/__pycache__`
- `tests/helpers/__pycache__`
- `trading_bot/__pycache__`
- `trading_bot/analytics/__pycache__`
- `trading_bot/app/__pycache__`
- `trading_bot/backtest/__pycache__`
- `trading_bot/config/__pycache__`
- `trading_bot/core/__pycache__`
- `trading_bot/execution/__pycache__`
- `trading_bot/positions/__pycache__`
- `trading_bot/risk/__pycache__`
- `trading_bot/storage/__pycache__`
- `trading_bot/storage/repositories/__pycache__`
- `trading_bot/strategy/__pycache__`

## Deleted Files

Top-level generated/runtime files deleted:

- `FULL_PATCH_EXPORT.md`
- `state.json`

Tracked generated files removed with their parent generated directories:

- `loader_smoke_tmp/bot.db`
- `loader_smoke_tmp/market_data/XAUUSD_15min.csv`
- `playback_smoke_modes/V1_BASELINE.db`
- `playback_smoke_tmp/bot.db`
- `tmp_control_debug/config.json`
- `reports/BOT_ARCHITECTURE_AND_PARITY_AUDIT.md`
- `reports/BOT_EXECUTIVE_SUMMARY.md`
- `reports/backtest_live_discrepancy_report.md`
- `reports/backtest_live_divergence_report.md`
- `reports/blocked_reasons.csv`
- `reports/close_reasons.csv`
- `reports/daily_report_2026-04-12.md`
- `reports/daily_report_2026-04-14.md`
- `reports/daily_report_2026-04-15.md`
- `reports/daily_report_2026-04-16.md`
- `reports/daily_report_2026-04-17.md`
- `reports/daily_report_2026-04-19.md`
- `reports/daily_report_2026-04-20.md`
- `reports/daily_report_2026-04-21.md`
- `reports/daily_report_2026-04-23.md`
- `reports/daily_summary.csv`
- `reports/execution_reasons.csv`
- `reports/oos_eval_0002_20260419_202810.zip`
- `reports/oos_eval_0002_20260419_202810/blocked_reason_breakdown.csv`
- `reports/oos_eval_0002_20260419_202810/comparison_methodology.md`
- `reports/oos_eval_0002_20260419_202810/daily_consistency.csv`
- `reports/oos_eval_0002_20260419_202810/deferred_conversion.csv`
- `reports/oos_eval_0002_20260419_202810/evaluation_summary.md`
- `reports/oos_eval_0002_20260419_202810/executed_rate_by_strategy.csv`
- `reports/oos_eval_0002_20260419_202810/execution_quality.csv`
- `reports/oos_eval_0002_20260419_202810/exit_reason_distribution.csv`
- `reports/oos_eval_0002_20260419_202810/family_contribution.csv`
- `reports/oos_eval_0002_20260419_202810/family_regime_breakdown.csv`
- `reports/oos_eval_0002_20260419_202810/family_session_breakdown.csv`
- `reports/oos_eval_0002_20260419_202810/family_volatility_breakdown.csv`
- `reports/oos_eval_0002_20260419_202810/management_quality.csv`
- `reports/oos_eval_0002_20260419_202810/portfolio_summary.csv`
- `reports/oos_eval_0002_20260419_202810/recommended_charts.md`
- `reports/oos_eval_0002_20260419_202810/report.json`
- `reports/oos_eval_0002_20260419_202810/scenario_comparison.csv`
- `reports/oos_eval_0002_20260419_202810/scenario_manifest.csv`
- `reports/oos_eval_0002_20260419_202810/score_distribution.csv`
- `reports/oos_eval_0002_20260419_202810/session_consistency.csv`
- `reports/oos_eval_0002_20260419_202810/signal_funnel.csv`
- `reports/oos_eval_20260418_083900/blocked_reason_breakdown.csv`
- `reports/oos_eval_20260418_083900/comparison_methodology.md`
- `reports/oos_eval_20260418_083900/daily_consistency.csv`
- `reports/oos_eval_20260418_083900/deferred_conversion.csv`
- `reports/oos_eval_20260418_083900/evaluation_summary.md`
- `reports/oos_eval_20260418_083900/executed_rate_by_strategy.csv`
- `reports/oos_eval_20260418_083900/execution_quality.csv`
- `reports/oos_eval_20260418_083900/exit_reason_distribution.csv`
- `reports/oos_eval_20260418_083900/family_contribution.csv`
- `reports/oos_eval_20260418_083900/family_regime_breakdown.csv`
- `reports/oos_eval_20260418_083900/family_session_breakdown.csv`
- `reports/oos_eval_20260418_083900/family_volatility_breakdown.csv`
- `reports/oos_eval_20260418_083900/management_quality.csv`
- `reports/oos_eval_20260418_083900/portfolio_summary.csv`
- `reports/oos_eval_20260418_083900/recommended_charts.md`
- `reports/oos_eval_20260418_083900/report.json`
- `reports/oos_eval_20260418_083900/scenario_comparison.csv`
- `reports/oos_eval_20260418_083900/scenario_manifest.csv`
- `reports/oos_eval_20260418_083900/score_distribution.csv`
- `reports/oos_eval_20260418_083900/session_consistency.csv`
- `reports/oos_eval_20260418_083900/signal_funnel.csv`
- `reports/setup_performance.csv`
- `reports/strategy_runtime_report_2026-05-01.md`

Also deleted untracked generated files under the removed runtime directories, including `*.db`, `*.log`, `*.zip`, `*.pyc`, `*.pyo`, and generated smoke/test output files.

## Reviewed But Kept

- `AUDIT_AND_FIX_REPORT.md`: kept as a historical remediation and validation record.
- `PROJECT_CONTEXT.md`: kept as useful repository and architecture context.
- `docs/CODEBASE_CLEANUP_REPORT.md`: kept as cleanup documentation from the previous phase.
- `docs/DETERMINISTIC_BASELINE_REPORT.md`: kept as deterministic-baseline documentation.
- `pytest.ini`: kept because it defines the `fast` and `replay` markers used by validation.
- `tests/fixtures/golden_replay_expected.json`: kept as the golden replay baseline.
- `tests/helpers/`: kept because deterministic tests depend on it.
- `tests/test_deterministic_baseline_contracts.py`: kept as the fast deterministic contract suite.
- `tests/test_backtest_live_like_parity.py`: kept as the replay/parity regression file.
- `.env`: kept locally, ignored by git, because it may contain operator-specific secrets.
- `.env.example`: kept as the sanitized environment template.

## Final High-Level Structure

Root now contains source, config, documentation, tests, and launch helpers only:

- `.claude/`
- `.git/`
- `dashboard/`
- `docs/`
- `scripts/`
- `services/`
- `tests/`
- `trading_bot/`
- `tradingbot/`
- `.env`
- `.env.example`
- `.gitignore`
- `AUDIT_AND_FIX_REPORT.md`
- `PROJECT_CONTEXT.md`
- `README.md`
- `config.json`
- `filters.py`
- `indicators.py`
- `lebprim_backtest_runner.py`
- `lebprim_strategy.py`
- `logger.py`
- `main.py`
- `mt5_connector.py`
- `pytest.ini`
- `requirements.txt`
- `risk_manager.py`
- `strategy.py`
- `strategy_factory.py`
- `utils.py`
- Windows launch/sync helpers: `Start Bot.bat`, `Start Dashboard.bat`, `Start Dashboard Hidden.bat`, `Pull from GitHub.bat`, `Push to GitHub.bat`

Post-validation residue scan found no `__pycache__` directories and no `*.pyc`, `*.pyo`, `*.db`, `*.db-wal`, `*.db-shm`, `*.sqlite`, `*.sqlite3`, `*.log`, or `*.zip` files outside `.git`.

## Validation Results

- Compile: `python -m compileall .` passed.
- Fast deterministic gate: `python -m pytest -q -m fast` passed with `18 passed, 189 deselected, 1 warning in 4.62s`.
- Golden replay contracts: `python -m pytest -q tests/test_deterministic_baseline_contracts.py` passed with `18 passed, 1 warning in 1.95s`.
- Parity file: `python -m pytest -q tests/test_backtest_live_like_parity.py` passed with `15 passed, 12 warnings in 52.78s`.
- Full suite: `python -m pytest -q` passed with `207 passed, 2827 warnings in 319.80s (0:05:19)`.

## Behavior Confirmation

This pruning pass did not edit production trading source files. It only removed generated/runtime artifacts and added this report. Source trading behavior is unchanged by the pruning pass.
