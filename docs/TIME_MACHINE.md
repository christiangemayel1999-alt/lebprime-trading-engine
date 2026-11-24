# Time Machine: odds for one zone

Indicator Lab tab → **Time Machine: odds for one zone** (under the Strategy test panel).

## How to use it

1. Pick a zone from **Live bot zone** (● = still waiting or active). The boxes fill in by themselves:
   symbol, timeframe, side, Entry 1, stop loss, zone top/bottom and the time the LIMIT was placed.
   You can also type any zone yourself; leave the time empty to mean "now".
2. Press **Check the odds**. The run is queued in the research sandbox (same queue as Strategy tests,
   one job at a time). The first run for a symbol + timeframe builds its zone library with the bot's
   engine (about 30 s for gold M30, 1-2 minutes for M15, 8-10 minutes for M1); later runs reuse it for 7 days.
3. Read the result:
   * **(a) Similar past zones**: how past zones like this one did with your execution. The headline is
     the strictest match that still has at least 30 trades. The table goes from strict (same side,
     similar SL in ATR, same H1 trend, same session, same zone width) to loose (every zone of that side).
     Each row: trades, win % with its 95% range, net R, avg R per trade, avg win / avg loss, the win %
     you need to break even, costs, max drawdown, and how trades ended (TP / SL / closed after 3 days).
     Year by year for the headline row, and every zone of the symbol/timeframe split into buys vs sells.
   * **(b) Monte Carlo**: 10,000 three-day price paths built from the last ~20 trading days of real M1
     candles before the LIMIT, all starting at the fill. It counts how often TP is touched before SL.
     The headline removes the recent drift (so it does not just extend last week's trend); the "with the
     recent drift kept" number is shown next to it. With TP at about 1R this is close to 50% by geometry;
     the useful part is when it moves clearly away from the break-even win %.
   * **Chance price reaches Entry 1** (live zones only): how often price touches Entry 1 before the
     LIMIT expires after 360 candles.
   * **What actually happened** (old zones only): the real outcome, shown separately and never used for the odds.

## Rules it follows

* Your execution: one LIMIT at Entry 1, zone SL, TP = Entry 1 ± (1R + $0.20 on gold, 2× the median
  spread elsewhere). Touch-triggered on M1 with the real candle spread (buys fill on the ask and exit
  on the bid), a stop wins a same-minute tie, round-trip commission charged, closed at market after 3 days.
* No look-ahead: indicators come from candles that had closed when the LIMIT was placed, analogs only
  count trades that had already exited by then, and the Monte Carlo only uses earlier candles.
* Zones come from the bot's own engine with the live bot's parameters; untouched LIMITs are cancelled
  after 360 candles like in the live bot (it only scans its last 360 candles).
* Data: closed M1 candles from `/var/lib/lebprime/mt5_data/M1` (updated hourly by `lebprime-mt5-pull`).
* The dashboard only writes `reports/dashboard_tests/<id>/request.json` (`"kind": "time_machine"`);
  `lebprime-research-dispatch` runs it in `lebprime-research`. Nothing touches MT5, the live bot or orders.

Code: `services/indicator_lab/time_machine.py` (rules), `scripts/time_machine_job.py` (job),
`dashboard/time_machine.py` (routes), `dashboard/static/time_machine.js` (panel), `tests/test_time_machine.py`.
