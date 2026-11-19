# LebPrime v3.4.0 Live/Demo Runtime

`lebprime_v3_4_0` is a separate, disabled-by-default strategy. It does not use
the legacy `lebprim_scalp` implementation. Each logical symbol/timeframe pair
owns an independent `LebPrimeEngine` from Indicator Lab, so momentum, compact
base, refinement, zone clamp, confirmation, structural stop, and TP lifecycle
semantics remain shared with the validated v3.4.0 implementation.

## Symbols and contexts

- `XAUUSD` -> `XAUUSD`, buffer `1.0`
- `US30` -> `US30`, buffer `30.0`
- `NAS100` -> `USTEC`, buffer `15.0`
- Timeframes: `M1`, `M3`, `M5`, `M15`, `M30`, `M45`

The default matrix contains 18 isolated contexts. Strategy calculations and IDs
always use logical symbols; only market-data and order adapters use broker names.

## M45 candles

Native MT5 M45 is used only when the installed MT5 package exposes a numeric
`TIMEFRAME_M45`. Otherwise, closed M15 candles are aggregated in groups of three.
Boundaries are fixed to UTC midnight (`00:00`, `00:45`, `01:30`, ...). A candle
is emitted only when opens at `+0`, `+15`, and `+30` minutes are all present and
the 45-minute period has closed. MT5 timestamps are normalized to UTC before
aggregation, preventing session timezone or future-candle drift.

## Execution safety

Historical candles warm each engine without creating executable families. Only
new closed candles can produce live actions. Cross-timeframe duplicate detection
runs before risk sizing. The first overlapping same-side LIMIT owns the family;
later members remain in the theoretical engine lifecycle with
`DUPLICATE_SUPPRESSED` execution status.

LIVE mode requires MT5 `account_info.trade_mode` to equal
`ACCOUNT_TRADE_MODE_DEMO`. Missing or ambiguous account mode returns
`ACCOUNT_NOT_CONFIRMED_DEMO`. Entry1 and Entry2 are GTC LIMIT orders, each sized
by the existing risk manager with `0.5` risk adjustment so the pair does not
double total configured risk.

## Execution model (Indicator Lab and live engine)

`execution_model` selects how the engine settles a signal:

- `Live broker` (default) mirrors the MT5 runtime: Entry 1 and Entry 2 are resting
  LIMIT orders that fill on any touch after the LIMIT CREATED candle (at the open on a
  gap); SL and TP2 trigger on a touch, including on the fill candle; when SL and a target
  share a candle, SL is assumed first; the TP1 half-close and the post-TP1 return to
  Entry 1 exit at the candle close, as the bot acts on closed candles. Each entry carries
  half of the configured risk, so a full stop is -1R. Win rate counts trades with
  `result_r > 0`; `tp_touch_rate` keeps the old TP-touch figure.
- `Pine indicator` reproduces the TradingView v3.4.0/v3.4.1 scoring (body-close SL,
  TP priority, fixed +1R/+2R results) for chart comparisons.

Spread and commission are not deducted yet.

## Broker symbols

Selectable logical -> MT5 symbols: XAUUSD -> XAUUSD, US30 -> US30, NAS100 -> USTEC,
XAGUSD -> XAGUSD, WTI -> WTI, EURUSD -> EURUSD. Without a `symbols` list in config the
runtime still defaults to XAUUSD, US30 and NAS100. EURUSD uses the Pine forex SL buffer
(0.0005). Indicator Lab datasets drop the still-forming last candle when frozen.

## Stop-loss rule (phase 2)

Default `stop_rule = Candle close (Pine)`: like the TradingView indicator, a trade is closed only
when a candle closes with at least half its body beyond the SL. A wick through the SL does not close
it. The bot acts on closed candles, so the exit price is that candle's close, and a loss can be larger
than 1R. The SL is judged from the candle after Entry 1 fills, as in Pine.

Because the real SL is only enforced at candle close, MT5 holds a wider emergency stop:
`hard_stop_beyond_r` x risk beyond the SL (default 1.0, i.e. 2R from Entry 1). It covers gaps and the
bot being offline. `0` puts the broker stop at the structural SL. `Price touch` restores the plain
broker stop at the SL.

`leg_sizing` defaults to `Entry 1 distance (equal lots)`: both entries are sized from Entry 1's risk,
so a close beyond the SL cannot turn Entry 2 (sized on a much smaller distance) into a multi-R loss.
