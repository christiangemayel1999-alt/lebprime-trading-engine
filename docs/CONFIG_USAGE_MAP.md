# Configuration Usage Map

This map classifies the current `config.json` contract by actual code consumers. A section can serve more than one runtime; the classification names its primary ownership and lists secondary consumers explicitly. No keys were removed in Phase 3.

## Classification summary

| Classification | Sections / keys | Primary consumers |
|---|---|---|
| `LIVE_LEBPRIME` | `lebprime_v3_4_0`, relevant `risk`, `exit`, and `mt5` values | `LebPrimeLiveService._strategy_config()`, `_selected_symbols()`, `_selected_timeframes()`, `_preflight()`, `RiskManager`, `MT5Connector` |
| `LIVE_INFRASTRUCTURE` | `bot`, `runtime`, `mt5`, `broker`, `risk`, `exit`, `cooldowns`, `safety`, `storage`, `logging`, `journaling`, `telegram`, `integrations`, `dashboard`, `analytics`, `position_sizing` | `TradingBot`, `ConfigManager`, `ControlStateService`, `MT5Connector`, `RiskManager`, `TradeJournalService`, Telegram and dashboard services |
| `BACKTEST_RESEARCH` | `backtest`, `indicators`, `regime`, `sessions`, `entry`, `shorts`, `validation`, plus research-selected portions of `strategy`, `execution`, `risk`, and `exit` | `BacktestRunner`, `ReplaySession`/`StrategyReplayEvaluator`, `UnifiedStrategyEngine`, `OOSService`, Indicator Lab services |
| `LEGACY_COMPATIBILITY` | `strategy.setup_controls`, `strategy.setup_families`, `strategy.lebprim_*`, top-level `strategies`, generic `symbols` and `timeframes` aliases | `strategy_factory`, `StrategyEngine`, `LebprimStrategy`, `UnifiedStrategyEngine`, execution-mode normalization, config/control compatibility routes |
| `UNUSED` | None proven safe at key level | Static references and compatibility normalization prevent classifying any current persisted section as unused with sufficient confidence. |

## High-priority key tracing

| Key | Classification | Exact consumers and responsibility |
|---|---|---|
| `lebprime_v3_4_0.enabled` | `LIVE_LEBPRIME` | `LebPrimeLiveService.enabled()` and startup/status gating in `main.py`. |
| `lebprime_v3_4_0.symbols` | `LIVE_LEBPRIME` | `LebPrimeLiveService._selected_symbols()`; resolved through `services.lebprime_live.symbols.resolve_symbol()`. It is separate from generic top-level `symbols`. |
| `lebprime_v3_4_0.timeframes` | `LIVE_LEBPRIME` | `LebPrimeLiveService._selected_timeframes()`; normalized through `services.lebprime_live.timeframes.normalize_lebprime_timeframe()`. |
| `lebprime_v3_4_0.parameters` | `LIVE_LEBPRIME` | Copied into each `LiveLebPrimeEngine` by `_parameters()`/`_build_contexts()`. This owns current Pine-parity zone calculations. |
| `strategy.setup_controls` | `LEGACY_COMPATIBILITY` + `BACKTEST_RESEARCH` | Read by `StrategyEngine`, strategy diagnostics, `BacktestRunner`, execution-mode normalization, `ConfigManager`, `ControlPlaneService`, and operator compatibility routes. `build_live_runtime_support_engine()` disables every family in a private copy. |
| `strategy.setup_families` | `LEGACY_COMPATIBILITY` + `BACKTEST_RESEARCH` | Read by `StrategyEngine`, `LebprimStrategy`, adapters/registry, backtests, diagnostics, and config/operator services. Not the source of truth for `LebPrimeLiveService`. |
| `strategy.lebprim_*` | `LEGACY_COMPATIBILITY` + `BACKTEST_RESEARCH` | Read by `LebprimStrategy`, `ExecutionDecisionEngine`, `PendingOrderPolicy`, and replay/backtest pending semantics. These keys configure the compatibility `LEBPRIM_SCALP` strategy, not the live v3.4.0 Pine engine. |
| `regime` | `BACKTEST_RESEARCH` / live support context | Read by legacy and Lebprim strategy context classification and normalized by `ConfigSchema`. Used by the non-trading support engine in live mode and by backtests. |
| `sessions` | `BACKTEST_RESEARCH` / live support context | Read by `StrategyEngine` and `LebprimStrategy` session classifiers and retained in the normalized shared schema. Main runtime diagnostics/context still consume the support-engine result. |
| `entry` | `BACKTEST_RESEARCH` / generic compatibility | Read by generic strategy/decision and execution paths for entry scoring/modes. The LebPrime v3.4.0 live service uses its own zone Entry1/Entry2 output instead. |
| `analytics` | `LIVE_INFRASTRUCTURE` | `TradingBot._initialize_analytics_services()` constructs optional decision logging, shadow testing, and correlation monitoring. |
| `position_sizing` | `LIVE_INFRASTRUCTURE` compatibility | `TradingBot._initialize_analytics_services()` optionally constructs `DynamicPositionSizer`; authoritative live order sizing remains in `RiskManager`/`RiskEngine` and the LebPrime preflight path. |
| `risk` | `LIVE_LEBPRIME` + shared execution/backtest | `RiskManager`, `RiskEngine`, live lifecycle, LebPrime preflight, and `BacktestRunner`. Do not split or remove without a unified risk contract. |
| `exit` | `LIVE_LEBPRIME` + shared execution/backtest | `RiskManager` and `TradeManagementProfileResolver` calculate/manage generic trade exits; LebPrime also consumes configured risk/exit support while retaining its Pine-derived zone targets. |
| `execution` | `LIVE_INFRASTRUCTURE` + `BACKTEST_RESEARCH` | Spread policy, canonical execution decisions, exposure rules, MT5 adapter behavior, replay fill behavior, and pending policy. |
| `indicators` | `BACKTEST_RESEARCH` / support engine | `strategy.py`, `lebprim_strategy.py`, and `indicators.py`; not the calculation source for `LiveLebPrimeEngine`. |
| `backtest` | `BACKTEST_RESEARCH` | Backtest request defaults, fill model, storage/reporting, worker jobs, OOS scenario runs. |

## Alias and compatibility notes

`ConfigSchema.normalize()` intentionally maintains `runtime.mode`, `broker.mt5`, `integrations.telegram`, and top-level `strategies` aliases from their older source sections. Those aliases are not independent sources of truth and must not be deleted until every compatibility consumer has migrated.

Generic top-level `symbols`, `timeframes`, and `strategy.lebprim_*` do not select the live LebPrime v3.4.0 symbol/timeframe matrix. The live matrix is owned by `lebprime_v3_4_0` and the fixed mappings in `services/lebprime_live/symbols.py` and `timeframes.py`.

## Phase 4 configuration recommendation

Introduce a read-only typed configuration facade that exposes explicit `live_lebprime`, `live_infrastructure`, and `research` views while continuing to load the existing JSON shape. Measure access to compatibility aliases before attempting a migration. Remove keys only after stored configs, worker payloads, dashboard imports/exports, and external scripts have been migrated.
