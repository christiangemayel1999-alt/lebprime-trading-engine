# Deterministic Baseline Report

Date: 2026-09-08

## Test Architecture

Two pytest tiers are now defined in `pytest.ini`:

- `fast`: deterministic behavior contracts suitable for the normal development gate.
- `replay`: historical replay or integration-heavy tests that may take longer.

Recommended commands:

```bash
python -m pytest -q -m fast
python -m pytest -q -m replay
python -m pytest -q
```

The new fast suite is `tests/test_deterministic_baseline_contracts.py`. It locks core behavior with small frozen fixtures and direct calls into the existing canonical execution, replay broker, simulator, risk, and strategy components.

Existing runner-level parity tests in `tests/test_backtest_live_like_parity.py` are marked `replay`. They remain valuable integration coverage, but they should not be the only safety net for future refactors.

## Existing Parity Test Classification

`tests/test_backtest_live_like_parity.py`:

- `test_no_future_candles_in_signal_generation`: DETERMINISTIC REPLAY CONTRACT.
- `test_next_bar_open_fill_happens_after_signal_bar`: DETERMINISTIC REPLAY CONTRACT.
- `test_structure_break_min_bars_gate`: FAST UNIT CONTRACT.
- `test_structure_break_requires_consecutive_closes`: FAST UNIT CONTRACT.
- `test_partial_tp_not_double_counted`: FAST UNIT CONTRACT.
- `test_tp_sl_trade_closed_events_are_emitted`: DETERMINISTIC REPLAY CONTRACT.
- `test_summary_trades_playback_reconcile`: DETERMINISTIC REPLAY CONTRACT.
- `test_account_blown_stops_backtest`: DETERMINISTIC REPLAY CONTRACT.
- `test_insufficient_margin_rejects_trade`: DETERMINISTIC REPLAY CONTRACT.
- `test_friday_hard_close_closes_positions`: DETERMINISTIC REPLAY CONTRACT.
- `test_weekend_cutoff_blocks_new_entries`: DETERMINISTIC REPLAY CONTRACT.
- `test_duplicate_setup_fingerprint_blocked`: DETERMINISTIC REPLAY CONTRACT.
- `test_requested_vs_effective_data_range_recorded`: DETERMINISTIC REPLAY CONTRACT.
- `test_equity_curve_not_silently_truncated`: INTEGRATION TEST; TOO EXPENSIVE FOR NORMAL TEST GATE if implemented with per-row inserts.
- `test_live_backtest_shared_execution_plan_parity`: FAST UNIT CONTRACT.

The slowest issue was not the assertion itself. It was test fixture cost: full runner playback wrote/logged many frames, and the equity-curve test inserted 25,050 rows one transaction at a time. The replay logger is now muted inside the scripted helper, candle fixtures are smaller, and the large equity setup uses a batch insert while preserving the over-25k regression condition.

## Deterministic Fixtures

Created:

- `tests/helpers/deterministic_market.py`
- `tests/fixtures/golden_replay_expected.json`

Fixture helpers include:

- `candles(...)`: compact deterministic OHLCV sequence.
- `flat_no_signal_candles(...)`: stable no-signal market.
- `symbol_spec(...)`: deterministic broker symbol metadata.
- `candidate(...)`: deterministic strategy candidate payload.
- `canonical_plan(...)`: deterministic `CanonicalExecutionPlan`.

Covered scenarios:

- A: no signal.
- B: valid immediate entry.
- C: pending entry activation.
- D: pending never activates and expires.
- E: TP1 partial realization.
- F: TP2 final close.
- G: stop loss.
- H: TP1 then breakeven/SL, preserving TP1 realized PnL.
- I: duplicate setup conflict.
- J: Friday cutoff.
- K: margin rejection.
- L: account protection.

## No-Lookahead Verification

The fast suite directly verifies that strategy-visible M1 data never contains candles beyond the current replay timestamp.

It also locks the existing higher-timeframe close semantics from `BacktestRunner`: a higher-timeframe bar opened at `T` is visible only when the current trigger timestamp is greater than or equal to `T + timeframe_duration`. This protects M3/M15-style derived frames from exposing incomplete future higher-timeframe candles.

The runner-level replay test `test_no_future_candles_in_signal_generation` still verifies the same contract through `BacktestRunner` using the scripted strategy observation hook. That test now completes in seconds.

## Execution Timing

The fast suite documents the three supported backtest execution models:

- `next_bar_open`: signal on candle N cannot fill on candle N; earliest fill is candle N+1 open.
- `signal_price_touch`: fill occurs only if the eligible signal bar touches the requested entry price.
- `current_bar_close`: fill uses the current completed candle close.

These contracts are represented in `test_execution_model_timing_contracts` and `test_signal_price_touch_blocks_when_signal_bar_does_not_reach_entry`.

## Canonical LIVE/BACKTEST Parity

`CanonicalExecutionPlan.to_comparable_dict()` was added as pure serialization for broker-independent parity assertions. It compares:

- strategy name
- symbol
- side
- order type
- requested entry
- volume
- SL
- TP
- setup family
- setup fingerprint
- entry mode
- execution model
- signal/execution timestamps
- signal bar close
- executable entry
- spread/slippage
- fill side
- data available through time
- TP1
- trigger price
- risk metadata
- reason code

It intentionally ignores broker-specific/runtime-only data such as MT5 tickets, simulated IDs, runtime labels, and network details.

## Lifecycle Contract

Tested legal transitions:

- `PENDING -> ACTIVATED -> TP1 -> TP2`
- `PENDING -> ACTIVATED -> SL`
- `PENDING -> EXPIRED`
- direct market open -> terminal TP/SL

Invalid/guarded behavior:

- A pending order cannot activate on its placement bar.
- TP/SL cannot close a trade on the same timestamp as its open time.
- Terminal closed trades cannot close a second time through `on_bar`.

## Accounting Contract

The fast suite asserts:

- `starting_balance + realized PnL = ending balance`
- `ending balance + floating PnL = ending equity`
- TP1 partial PnL is recorded once.
- Remaining volume after TP1 is correct.
- Final realized PnL excludes already-realized TP1.
- Commission and swap are applied to the final close payload according to current simulator behavior.

The runner-level parity suite also reconciles backtest trades, summary, equity frames, and playback storage.

## Intrabar Ambiguity

The current simulator has an explicit deterministic same-bar TP/SL policy:

- default `same_bar_rule`: `sl_first`
- optional configured rule: `tp_first`

The fast suite locks both behaviors. No new favorable ordering was invented.

## Golden Replay

Golden fixture: `tests/fixtures/golden_replay_expected.json`.

Expected result:

```json
{
  "scenario": "canonical_simulated_executor_golden_replay",
  "candidates": 1,
  "executable_signals": 1,
  "pending_orders": 1,
  "activated_trades": 1,
  "trade_lifecycle_events": ["pending_created", "pending_filled", "trade_partial_close", "trade_closed"],
  "tp1_count": 1,
  "tp2_count": 1,
  "sl_count": 0,
  "starting_balance": 10000.0,
  "ending_balance": 10001.5,
  "ending_equity": 10001.5,
  "realized_pnl": 1.5,
  "max_drawdown": 0.0
}
```

This is intentionally small and human-readable. It protects canonical replay/lifecycle/accounting semantics rather than historical performance.

## Test Performance

Measured on this workspace:

- `python -m compileall .`: passed.
- `python -m pytest -q -m fast`: 18 passed, 189 deselected, 1 warning in 5.89s.
- `python -m pytest -q tests/test_deterministic_baseline_contracts.py`: 18 passed, 1 warning in 1.37s.
- `python -m pytest -q tests/test_backtest_live_like_parity.py::test_no_future_candles_in_signal_generation`: 1 passed, 2 warnings in 4.94s.
- `python -m pytest -q tests/test_backtest_live_like_parity.py`: 15 passed, 12 warnings in 52.80s.
- `python -m pytest -q`: 207 passed, 2827 warnings in 312.93s.

The full suite slowdown is expected integration/runtime cost, not a hang. Warning volume is dominated by Python 3.14 deprecations in `pytest_asyncio`, Starlette/FastAPI coroutine checks, and `datetime.utcnow()` usage in project code.

## Known Behavioral Discrepancies

- Severity P2: naming divergence in margin rejection. `RiskManager.fit_volume_to_margin` currently returns `reason_code="margin_constraint"` for insufficient margin, while `BacktestRunner` records the blocked signal as `insufficient_margin`. This was preserved and tested as current behavior.
- Severity P2: Friday/weekend behavior is split between runner cutoff helpers and broader session/risk configuration. The fast test locks the runner's Friday cutoff helper; the replay test locks the current `blocked_weekend_cutoff` behavior.
- Severity P3: replay export code emits `datetime.utcnow()` deprecation warnings on Python 3.14. This is not a trading behavior issue but should be cleaned later.

No discrepancy required a trading behavior change in this phase.

## Remaining P0/P1 Issues

P0:

- No unresolved P0 trading behavior ambiguity was introduced. Intrabar ambiguity has an explicit current rule and is now tested.

P1:

- `BacktestRunner.run` remains too large and mixes replay orchestration, strategy evaluation, risk, execution, accounting, playback, and report generation.
- Full-suite runtime is about five minutes in this workspace, so the fast marker should be used before refactors and the full suite before promotion.
- Live/backtest lifecycle parity is improved by canonical tests but still depends on separate live and replay implementations.

## Trading Behavior

Intentional trading behavior changes: NONE
