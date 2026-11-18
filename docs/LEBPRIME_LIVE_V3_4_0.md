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
