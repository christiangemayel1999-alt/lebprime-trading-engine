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

## Automatic odds and Telegram (added 26 Nov 2026)

* The dashboard checks the live bot's zones every minute (read-only). Each new zone that is waiting or active
  (not historical replays, created in the last 2 hours) gets a Time Machine run queued automatically. Runs show an
  **auto** badge in the panel. Two zone ids with the same symbol, timeframe, side, Entry 1 and SL are checked once.
  Automatic runs can queue up to 12 at a time and never block a run you start yourself.
* When a run finishes (automatic or yours), one Telegram message goes to the bot's chat: the zone, the odds with
  the bot's current exit first, the other exit second, a verdict, the fill chance and the conditions.
* Both exits are now computed: **TP 1R + extra** and the **bot's trailing exit** (from the live config: start at
  +1R, 1 ATR behind the best price, stop moved at candle closes and applied from the next candle). The panel has
  a switch; it opens on the exit the bot uses now.
* Zones come from the engine with the live bot's own parameters (storage/config.json + TradingView zone parity
  + 500-candle window), so the library contains the same zones the bot trades.
* Turn things off in config.json: `"time_machine": {"auto_odds": false}` or `{"telegram": false}`;
  `"timeframes": ["M5", "M15"]` limits automatic runs to those timeframes.
* Nothing runs until the dispatcher is on: `sudo systemctl enable --now lebprime-research-dispatch.timer`.

## The decision on every zone and trade (added 26 Nov 2026)

* When a run finishes, its decision (verdict for the bot's current exit, similar zones, win %, R per trade, Monte Carlo,
  fill chance) is saved against the live zone id in `storage/time_machine_decisions.json`.
* **Trades tab**: new "Time Machine" column with the decision for each trade's zone.
* **Telegram**: "LIMIT placed", "filled" and "closed" messages get a line such as
  `🔮 TM ❌ Negative odds · 44 zones 36% win -0.12R · fill 99%` (or "no decision yet" if the check is still running).
* **Time Machine panel**: "Every live zone and the Time Machine's decision (last 7 days)" with each zone's state,
  whether it was traded, the decision and the result the bot recorded, plus a scorecard of verdict vs result.
  "not checked" = the zone closed without a check (e.g. it was created before automatic checks started).
* Display and logging only: the bot never reads these decisions.
