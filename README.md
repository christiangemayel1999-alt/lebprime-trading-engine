# XAUUSD MT5 Score-Based Scalping Bot

## What Changed
This bot has been refactored into a production-style MT5 system for XAUUSD with:

- 15m EMA50 / EMA200 directional bias
- 3m pullback scoring instead of brittle yes/no setup checks
- 1m breakout trigger scoring
- hard risk brakes only for spread, ATR, cooldowns, drawdown, and open-trade limits
- 0.5% fixed-fractional risk sizing
- 1R partial TP, 2R final TP, breakeven at 1R, trailing from 1.2R
- Telegram alerts
- structured JSON-style cycle logs
- rotating log files for Windows VPS use
- safer state persistence for OneDrive / Windows file locking

## Strategy Summary
The bot evaluates every cycle in three layers:

1. `15m trend`
   - scores bullish and bearish trend quality from price vs EMA50 / EMA200, EMA separation, and EMA slope
2. `3m setup`
   - scores pullback quality near EMA20 / EMA50, candle rejection, RSI, ADX, and volume stability
3. `1m trigger`
   - scores breakout confirmation from candle body, close location, recent micro-range break, and volume expansion

Trades are only allowed when:

- M3 ATR is between `3.0` and `12.0`
- spread is `<= 25`
- only one trade is open
- session is open
- no blackout window is active
- cooldown and loss-lock rules are clear
- combined entry score passes the configured threshold

## Core Files
- `main.py`
- `strategy.py`
- `risk_manager.py`
- `filters.py`
- `indicators.py`
- `logger.py`
- `utils.py`
- `config.json`
- `requirements.txt`

## Setup
1. Install Python 3.11 on Windows.
2. Install MetaTrader 5 desktop terminal.
3. Copy `.env.example` to `.env`.
4. Fill in:
   - `MT5_LOGIN`
   - `MT5_PASSWORD`
   - `MT5_SERVER`
   - `MT5_PATH`
   - optional `TELEGRAM_BOT_TOKEN`
   - optional `TELEGRAM_CHAT_ID`
5. Install dependencies:
   ```powershell
   py -3.11 -m pip install -r requirements.txt
   ```

## Run Locally
Visible console:
```powershell
.\Start Bot.bat
```

Direct Python:
```powershell
py -3.11 main.py
```

Silent background-style launcher:
```powershell
scripts\start_bot.bat
```

Pull the latest code from Git:
```powershell
.\Pull from GitHub.bat
```

## Logs And State
- `logs\bot.log`
- `logs\errors.log`
- `logs\signals.csv`
- `logs\trades.csv`
- `storage\state.json`
- `storage\bot.db`

`bot.log` now contains structured cycle snapshots with score and reason details.

## Windows VPS Deployment
1. Put the project in a stable local path such as `C:\TradingBot`.
   Avoid running directly from OneDrive if possible.
2. Open MT5 once manually and log in.
3. Enable algorithmic trading inside MT5.
4. Install Python and run:
   ```powershell
   py -3.11 -m pip install -r requirements.txt
   ```
5. Configure `.env`.
6. Test with:
   ```powershell
   .\Start Bot.bat
   ```
7. If stable, use:
   ```powershell
   scripts\install_tasks.ps1
   ```
   to create scheduled tasks for MT5, the bot, and the dashboard.

## Dashboard
Run:
```powershell
py -3.11 -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8501
```

Or:
```powershell
scripts\start_dashboard.bat
```

Or from the project root:
```powershell
.\Start Dashboard.bat
```

Hidden/minimized attempt:
```powershell
.\Start Dashboard Hidden.bat
```

The dashboard is now a FastAPI control plane for the real bot, not a read-only toy monitor. It can:

- start and stop the live bot process
- pause or resume execution while keeping signal generation active
- switch between `DRY_RUN`, `DEMO`, `VALIDATION_TEST`, and `LIVE`
- toggle the kill switch and auto execution
- open and manage manual MT5 trades from the dashboard
- edit setup family controls, strategy settings, risk, sessions, symbols, and alerts
- apply one-click `SAFE_MODE`, `BALANCED_MODE`, and `SCALPING_MODE` presets
- preview config diffs before saving
- create timestamped backups in `storage\config_backups\`
- persist shared runtime controls in `storage\control_state.json`
- show logs, audit history, heartbeat, and recent execution telemetry
- optionally require HTTP Basic auth with `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`
- run in readonly mode for monitoring-only access

How it works:

- Dashboard writes validated config updates through a safe config manager.
- Every config save creates a backup before overwrite.
- Bot runtime reads `storage\control_state.json` every cycle to apply pause, stop, kill-switch, and reload requests.
- Switching to `LIVE` requires typing `LIVE` in the UI confirmation prompt.
- Many config changes take effect without restarting; when config is saved, the dashboard also requests a runtime reload.
- If `dashboard.readonly_mode` is enabled, control and config-mutating routes are blocked server-side.
- If `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` are set, the dashboard prompts for credentials before loading.
- Manual dashboard trades and Telegram remote commands share the same backend validation and MT5 execution path.
- On this Windows setup, the most reliable startup path is still the visible launcher or direct Python command. Hidden/background launchers may depend on local environment behavior.

## Telegram
If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are present, the bot sends:

- startup alerts
- scored setup alerts
- trade open alerts
- trade close alerts
- daily summaries
- critical error alerts

Optional remote control is also available:

- set `telegram.remote_control_enabled` to `true`
- set `telegram.admin_chat_ids` and/or `telegram.admin_user_ids`
- dangerous commands require `/confirm <token>` within the configured TTL
- supported commands include `/status`, `/startbot`, `/stopbot`, `/pause`, `/resume`, `/live`, `/dry`, `/killswitch_on`, `/killswitch_off`, `/positions`, `/close <ticket>`, `/closeall`, `/strategy <name> on|off`, `/family <name> on|off`, `/preset <name>`, and optional `/buy` / `/sell`

## Notes
- The bot is less strict than before, but not looser on risk.
- ATR values around `5.9` to `6.2` and spreads around `17` to `19` are now tradable by default.
- Spread spikes above `25` still block entries.
- The old 1m trigger timing trap has been removed, so the bot no longer depends on catching a candle within a 2-5 second window.
