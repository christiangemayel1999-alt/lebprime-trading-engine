# LebPrime Trading Engine

Automated MT5 trading bot for the LebPrime v3.4.0 zone strategy, with a web dashboard, Telegram alerts
and a research lab. It runs on a Linux VPS against a **demo** account (OneRoyal-Demo).

- Repository: `github.com/christiangemayel1999-alt/lebprime-trading-engine`
- Dashboard: `https://bot.lebprimesignals.com` (login required)
- Live system: Linux VPS, deployed automatically from `main`

> Never commit secrets. Passwords and tokens live only in `.env` on the PC and `/etc/lebprime/` on the VPS.

## How the system fits together

The code is organised in seven sections. Today several of them still share the `services/` folder; a
cleanup that gives each section its own folder is planned.

| Section | What it does | Where it lives now |
| --- | --- | --- |
| 1. Dashboard front end | Pages, buttons, charts | `dashboard/static/`, `dashboard/templates/` |
| 2. Dashboard back end | API behind the dashboard, bot controls, config editing | `dashboard/*.py`, `services/` (bot_control, control_plane, operator, config_manager, job_service) |
| 3. Trading engine / MT5 | Signals, risk, orders, positions, MT5 connection | `main.py`, `mt5_connector.py`, `risk_manager.py`, `trading_bot/`, `services/lebprime_live/` |
| 4. Market data | Candle history and datasets | `services/history_loader.py`, `services/indicator_lab/dataset_service.py` |
| 5. Research lab | Indicator Lab, strategy tests, Time Machine, backtest scripts | `services/indicator_lab/`, `services/worker.py`, `scripts/` |
| 6. Notifications / logging | Telegram, logs, audit trail | `services/telegram_*`, `logger.py`, `services/audit_service.py` |
| 7. Ops | VPS install, systemd services, auto-deploy, backups, sandbox | `deploy/linux/` |

Shared by everything: `utils.py` (config loading), `services/database.py` (SQLite), `trading_bot/core/`, `config.json`.

## Strategy: LebPrime v3.4.0

- Section `lebprime_v3_4_0` in the config. Each symbol and timeframe pair runs its own engine, the same
  engine the Indicator Lab uses for backtests (`services/indicator_lab/lebprime_engine.py`). An edit to that
  file changes live trading.
- Symbols on the VPS: XAUUSD, US30, NAS100 (broker USTEC), XAGUSD, WTI, EURUSD.
  Timeframes: M1, M3, M5, M15, M30, M45 (M45 is built from M15 candles when MT5 has no native M45).
- Zones match TradingView's chart zones (`tv_zone_parity`, `zone_window_bars` 500).
- Current forward-test exit (since 25 Nov 2026): 1 entry at Entry 1 (`entry2_mode` Off), fixed broker
  stop loss at the structural SL (1R), no fixed TP. From +1R a trailing stop follows the best price
  1.0 x ATR(14) behind it, moved at candle closes (`exit_mode` Trail, `trail_start_r` 1.0, `trail_mode` ATR,
  `trail_atr_mult` 1.0). The snippet is in `deploy/lebprime_v3_4_0.v340_forward.json`.
- Details: `docs/LEBPRIME_LIVE_V3_4_0.md`, `docs/TV_PARITY_TRAIL_REPORT.md`, `docs/PINE_PARITY_NOTES.md`.
  Pine references are in `docs/indicator_reference/`.

## Live system on the VPS

| Service | Job |
| --- | --- |
| `lebprime-bot` | The trading bot (`main.py`). Only this service can reach MT5. |
| `lebprime-mt5-gateway`, `lebprime-mt5-terminal` | MT5 terminal under Wine and the gateway the bot talks to |
| `lebprime-dashboard` | Dashboard on 127.0.0.1:8501, published through Caddy (HTTPS, login) |
| `lebprime-worker` | Background jobs for the Indicator Lab |
| `lebprime-research-dispatch.timer` | Runs dashboard strategy tests and Time Machine jobs in the research sandbox |
| `lebprime-autodeploy.timer` | Auto-deploy (below) |
| `lebprime-watchdog.timer`, `lebprime-backup.timer` | Health watchdog and backups |

Paths on the VPS:

- Code: `/opt/lebprime/app` (installed by the auto-deploy; do not edit by hand)
- Live config: `/var/lib/lebprime/storage/config.json` (this one is used, not the repo `config.json`)
- Live data: `/var/lib/lebprime/storage/` (`bot.db`, `control_state.json`), reports in `/var/lib/lebprime/reports/`
- Secrets: `/etc/lebprime/` (root only)

Execution can be paused and resumed from the dashboard. That setting is kept in `storage/control_state.json`
and survives restarts and deploys.

### Auto-deploy

Every ~2 minutes the VPS checks GitHub `main`. When it changes, it:

1. copies the new commit into a staging folder, compiles it, checks the bot modules import and runs the
   gateway tests,
2. stops the bot, worker and dashboard, installs the code, starts them again,
3. waits 75 s; if a service is down or the bot restarted, it rolls back to the previous version.

The result is sent to Telegram. So **anything merged into `main` goes live within a few minutes.**
Work on a branch, open a pull request, and merge only when you want it live.
Setup and runbook: `deploy/linux/README.md`, `deploy/linux/RUNBOOK.md`.

### Research sandbox

Backtests and research jobs run with `sudo lebprime-research [--detach] <command>`: capped CPU and memory,
lowest priority, no secrets, and no access to MT5 or the live trading controls. Research workspaces live in
`/var/lib/lebprime/research/`. Known gap: the sandbox can still write `/var/lib/lebprime/storage`;
a fix is planned.

## Dashboard

`https://bot.lebprimesignals.com`. Main things it does:

- status, open positions and trades (read from an MT5 account snapshot), logs, audit history
- start/stop the bot, pause/resume execution, kill switch (Start/Resume ask for confirmation)
- edit strategy, risk, sessions, symbols and alerts, with a diff preview and a backup on every save
  (`storage/config_backups/`); some settings need a bot restart
- Indicator Lab: datasets, campaigns, **Strategy test** (runs in the research sandbox)
- **Time Machine**: odds for a zone from similar past zones plus a Monte Carlo of price paths, run
  automatically for each new live zone; the decision shows in the Trades tab and on Telegram.
  See `docs/TIME_MACHINE.md`.

## Telegram

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set, the bot sends startup, trade open/close, daily
summary, error and deploy messages, plus Time Machine odds per new zone. Remote commands (`/status`,
`/pause`, `/resume`, ...) need `TELEGRAM_REMOTE_CONTROL_ENABLED=1` and admin chat/user ids. Trade commands
(`/buy`, `/sell`, `/close`, `/closeall`) also need `TELEGRAM_MANUAL_TRADE_COMMANDS_ENABLED=1` (off by default).
Dangerous commands ask for `/confirm <token>`.

## Working on the PC (Windows)

The live bot runs on the VPS. The PC copy is for reading, editing and local tests.

**Do not use `Pull from GitHub.bat`.** It points at an old repository name and runs `git reset --hard`
plus `git clean -fdx`, which deletes `.env`, `storage\`, `reports\`, logs and the venv.

First time:

```powershell
cd "C:\Users\<you>\OneDrive\Desktop\Trading_Bot_Original"
git clone https://github.com/christiangemayel1999-alt/lebprime-trading-engine.git "Trading Bot"
cd "Trading Bot"
py -3.11 -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env    # then fill in MT5 and Telegram values
```

Moving from an old folder: keep a backup copy, clone as above into a new folder, then copy `.env`,
`storage\`, `reports\`, `backtests\`, `LebPrime_Results\` and your venv across.

Update later:

```powershell
git pull
```

`git pull` never touches `.env`, `storage\` or other ignored files.

Run locally: `Start Dashboard.bat` (dashboard on http://127.0.0.1:8501) and `Start Bot.bat`.
Don't run a second bot on the same MT5 account as the VPS.

## Tests

```bash
python -m pytest -q
```

On the VPS, run tests in a research copy inside the sandbox, never in `/opt/lebprime/app`.
Two tests about Windows venv paths (`test_python_resolver_prefers_proven_current_interpreter_over_launcher`,
`test_resolve_python_executable_prefers_repo_local_venv`) fail on Linux; that is expected.

## Rules

- Demo account only. Live execution stays as Christian sets it.
- Never push straight to `main`: branch, pull request, then merge.
- Strategy changes are tested first in the research sandbox: no look-ahead, real broker costs,
  year-by-year win % and net R per symbol, avg win/avg loss, buys vs sells.
