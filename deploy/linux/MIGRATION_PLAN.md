# Migration plan: Windows laptop → Linux VPS

Goal: run the LebPrime bot, dashboard and research worker on an Ubuntu 24.04 VPS, with MT5 under Wine,
without changing strategy or risk logic, and with the Windows setup kept intact until the VPS passes
acceptance.

**Defaults for the whole migration:** demo account only, execution paused, gateway trading gate
`disabled`, one research worker, strategy and risk settings unchanged, no 10% compounding.

## What changed in the app (and what did not)

| Change | Where | Effect on Windows |
|---|---|---|
| Switchable MT5 transport (`native` / `gateway` / `auto`) | `trading_bot/mt5_transport/` | none: `auto` imports `MetaTrader5` exactly as before |
| Connector imports MT5 through the transport | `mt5_connector.py` (import block only) | none |
| Trade journal uses `connector.mt5` instead of importing `MetaTrader5` | `services/trade_journal.py` (one line) | none |
| Gateway server (Windows Python under Wine) | `trading_bot/mt5_transport/gateway_server.py` | not used |
| Contract tests | `tests/test_mt5_gateway_transport.py` | adds 12 tests |

No strategy, risk, sizing, entry or exit code was touched.

### Gateway contract

The gateway serves: `initialize`, `login`, `shutdown` (no-op, the gateway owns the session), `version`,
`terminal_info`, `account_info`, `symbols_total/get`, `symbol_info`, `symbol_info_tick`, `symbol_select`,
`copy_rates_from/_from_pos/_range`, `copy_ticks_from/_range`, `orders_total/get`, `positions_total/get`,
`history_orders_total/get`, `history_deals_total/get`, `order_calc_margin/profit`, `order_check`,
`order_send`, plus `health` and `execution_status`. Anything else is refused.

* Results come back as namedtuples and numpy structured arrays, like the native package. Timestamps are
  passed through unchanged (MT5 server time, exactly as on Windows), so the connector's existing broker-time
  normalisation keeps working. Both sides run with `TZ=UTC`.
* `last_error()` returns the terminal's error after each call, or a gateway code: `-20001` unavailable,
  `-20002` timeout, `-20003` execution ambiguous, `-20004` rejected, `-20005` deadline passed,
  `-20010` trading disabled, `-20011` wrong account, `-20012` not demo, `-20013` terminal disconnected.
* Every request carries a deadline. A trade request that waited past its deadline is not sent.
* `order_send`: the client creates a request id; the gateway writes it to `executions.jsonl` (fsync) before
  calling MT5 and writes the result after. If the reply is lost, the client asks for that id's outcome
  instead of resending. A request that was in flight when the gateway died is marked `ambiguous` and is never
  sent again; the client returns `None` with `-20003`, the connector raises, and the watchdog sends a
  critical alert. Reconcile with the broker (`lebprime-ctl reconcile`) before resuming.

## Sequence

Each phase has an exit check. Do not start the next phase until it passes.

### 0. Baseline (Windows, before anything moves)
* Note the current commit, `config.json` hash, open positions and pending orders, and account balance.
* Run the reference backtest on Windows and keep its summary (used in phase 7 for the parity check).
* Exit: baseline numbers written down.

### 1. VPS preparation
* Create the VPS (README, "VPS"), add your SSH key, then run `install.sh`.
* Fill `/etc/lebprime/lebprime.env` and `/etc/lebprime/gateway.env`. Set `LEBPRIME_MT5_EXPECTED_LOGIN` to the
  **demo** login. Keep `TELEGRAM_REMOTE_CONTROL_ENABLED=0` (the laptop is still polling Telegram).
* Exit: `sudo ufw status` shows only 22, 80, 443; `ssh` with a password is refused; `sudo lebprime-ctl status`
  lists the units.

### 2. Wine compatibility gate (stop here if it fails)
1. `sudo systemctl start lebprime-xvfb lebprime-mt5-terminal`
2. Set a VNC password and open the screen through an SSH tunnel (RUNBOOK, "Looking at the MT5 screen").
   Log into the OneRoyal demo account, tick "save password". Check the ping in the status bar.
3. `sudo systemctl start lebprime-mt5-gateway`, then `sudo lebprime-ctl health`: `terminal_connected: true`,
   `account_is_demo: true`, `expected_login_ok: true`, ticks for XAUUSD.
4. Data: `sudo /opt/lebprime/venv/bin/python /opt/lebprime/deploy/scripts/gateway_smoke.py`
   reads account, symbol, ticks and M1/M5/H1/H4 candles through the gateway.
5. Demo orders: `sudo lebprime-ctl gate enabled`, then run the smoke script with `--demo-order` (0.01 lot buy,
   then close). Then `sudo lebprime-ctl gate disabled`.
6. Reboot the VPS (`sudo reboot`) and repeat steps 3 to 5 without touching VNC: the terminal must log in by itself.
* Exit: all six pass. Take a provider snapshot of the VPS now. If MT5 will not run reliably under Wine,
  stop and fall back to a Windows Server VPS (the Windows transport is unchanged, so that move is easy).

### 3. Transport and process changes (already done in this branch)
* The bot runs with `LEBPRIME_MT5_TRANSPORT=gateway`; tests in `tests/test_mt5_gateway_transport.py` pass.
* Exit: `pytest tests/test_mt5_gateway_transport.py` passes on the VPS venv.

### 4. Data transfer (about 2.6 GB without old DB backups)
* On the laptop, **stop the bot** (dashboard Stop, then check the window closed), then:
  `python deploy\linux\migrate_state.py export --out C:\lebprime-export --dry-run`, then without `--dry-run`.
  SQLite files are copied with the online backup API and integrity-checked; every file gets a SHA-256.
* Copy: `scp -r C:\lebprime-export youruser@VPS_IP:/tmp/`
* The laptop bot can be started again afterwards, still on the demo account, if you want it running meanwhile.
  Anything it writes after the export stays on the laptop.
* Exit: export finished without errors; bundle copied.

### 5. Path and state migration
* `sudo python3 /opt/lebprime/deploy/migrate_state.py import --bundle /tmp/lebprime-export --dry-run`
  Read the path map lines and the scrub actions, then run without `--dry-run`.
* It verifies all checksums, places files, rewrites Windows paths, sets `mt5.terminal_path` to null,
  deletes stale locks and `worker_status.json`, clears the PID and running flags, and sets execution paused.
* Exit: `ls /var/lib/lebprime/storage` shows `bot.db`; no `*.lock` files; `control_state.json` has
  `"execution_paused": true` and `"bot_pid": null`.

### 6. Progressive activation
a. **Research only:** `sudo systemctl start lebprime-dashboard lebprime-worker`. Open the dashboard (Caddy or
   SSH tunnel), log in, queue the reference backtest. Exit: it completes and matches the Windows baseline
   (phase 7, item 2).
b. **Read-only broker:** gateway gate stays `disabled`. `sudo systemctl enable --now lebprime-bot`. The boot
   guard pauses execution. Watch `sudo lebprime-ctl logs` for an hour: candles, signals and heartbeats, and
   any order attempt is refused at the gateway with `-20010`. Exit: heartbeat fresh in the dashboard,
   `lebprime-ctl reconcile` matches what the dashboard shows.
c. **Demo execution:** `sudo lebprime-ctl gate enabled`, then resume execution from the dashboard.
   Exit: the demo order lifecycle works end to end (entry, SL/TP, TP1 partial, close) and the journal matches
   the broker.

### 7. Acceptance (all must pass)
1. Research runs without native MT5 (worker has no gateway access).
2. Windows and Linux backtests of the same commit, data and config give the same trades and Net R.
3. Gateway contract matches the connector: `tests/test_mt5_gateway_transport.py` plus phase 2 smoke test.
4. Demo order lifecycle works (6c).
5. Execution is blocked when: the terminal is disconnected (stop the terminal), the account is wrong
   (set a different `LEBPRIME_MT5_EXPECTED_LOGIN`), or quotes are stale (the connector's
   `max_tick_age_seconds` check; the watchdog also alerts). Each must refuse orders and say why.
6. No duplicate orders on timeout or restart: `systemctl restart lebprime-mt5-gateway` while an order is
   being placed on demo, then check `lebprime-ctl reconcile` and the journal (contract tests cover the logic).
7. A reboot does not enable unreconciled trading: after `sudo reboot`, `execution_paused` is true.
8. Unauthenticated requests are blocked: `curl -i https://DOMAIN/api/config` → 401; a POST with a foreign
   `Origin` → 403; `/health` → 404; port 8501 and 18812 are not reachable from outside.
9. A backup restores cleanly: `sudo lebprime-backup run` then `sudo lebprime-backup verify`.
10. 48 market-open hours on demo with no unexplained restarts, alerts or journal mismatches.

### 8. Handover
* Stop the Windows bot and dashboard, disable `scripts\install_tasks.ps1` scheduled tasks if installed, and
  turn off Telegram polling there. Then set `TELEGRAM_REMOTE_CONTROL_ENABLED=1` on the VPS if you use it and
  restart the dashboard and bot. Only one machine may poll Telegram and only one may trade the account.
* Enable backups: fill `/etc/lebprime/backup.env`, `sudo systemctl enable --now lebprime-backup.timer`.
* Going live (a separate decision, not part of this plan): new `LEBPRIME_MT5_EXPECTED_LOGIN`,
  `LEBPRIME_GATEWAY_DEMO_ONLY=0`, and the same phase 6 steps again.

## Rollback

Until phase 8, the laptop is untouched: same folder, same `.env`, `LEBPRIME_MT5_TRANSPORT` unset, so it still
uses the native package. To roll back at any point: `sudo systemctl stop lebprime.target lebprime-bot` on the
VPS, then start the Windows bot. If the VPS traded the demo account meanwhile, reconcile first
(RUNBOOK, "Two machines may have traded").

## Known gaps and follow-ups (need your OK, outside deployment code)

1. **Dashboard Start/Stop buttons spawn or stop `main.py` directly** (`services/bot_control_service.py`). On the
   VPS, use Pause/Resume in the dashboard and `systemctl` for start/stop. Making the buttons only set a desired
   state for systemd is a small change to a shared file.
2. **`safe_order_send` retries on `TRADE_RETCODE_TIMEOUT` and `TRADE_RETCODE_CONNECTION`** (existing live
   logic, Windows too). Those retcodes come from the broker and the order may already have been filled, so a
   retry could duplicate it. The gateway cannot tell those retries apart from new orders. Recommended fix:
   check positions and orders for the same magic and comment before retrying. This is a live-bot change, so it
   waits for your approval.
3. `config.json` stays in the app folder because the app writes it there (sticky, group-writable folder keeps
   the code files protected). A `LEBPRIME_CONFIG_PATH` setting would let it live in `/etc/lebprime`.
4. The quote-freshness block relies on the connector's existing `max_tick_age_seconds`; the gateway does not
   add its own, because broker timestamps are in server time and a second, different check would disagree.
