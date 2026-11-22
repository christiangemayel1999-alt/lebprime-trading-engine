# LebPrime on a Linux VPS

This folder is everything needed to run the LebPrime MT5 bot on Ubuntu 24.04 instead of the Windows laptop.
Read [MIGRATION_PLAN.md](MIGRATION_PLAN.md) before doing anything with real accounts, and keep
[RUNBOOK.md](RUNBOOK.md) open when something goes wrong.

## How it works

The `MetaTrader5` Python package only exists for Windows, so the VPS runs two worlds side by side:

```
            Wine prefix (user lebprime-mt5)                    native Linux (user lebprime)
  ┌───────────────────────────────────────────────┐     ┌────────────────────────────────────────┐
  │ Xvfb :99 ── MT5 terminal64.exe ── broker      │     │ main.py (bot)  LEBPRIME_MT5_TRANSPORT=  │
  │                 │                             │     │   MT5Connector ── GatewayMT5 ──────────┼──┐
  │ Windows Python + MetaTrader5                  │     │ dashboard (127.0.0.1:8501) ← Caddy 443 │  │
  │   gateway_server.py  127.0.0.1:18812  ◄───────┼─────┼────────────────── HMAC-signed JSON ────┼──┘
  └───────────────────────────────────────────────┘     │ worker (research, CPU/RAM capped)      │
                                                        └────────────────────────────────────────┘
```

* `trading_bot/mt5_transport/` gives the bot a switchable MT5 module. `LEBPRIME_MT5_TRANSPORT=native` (or the
  default `auto`) imports `MetaTrader5` as before on Windows; `gateway` uses `GatewayMT5`, which has the same
  functions, constants and return shapes. `MT5Connector` and the strategy code are unchanged.
* The gateway is ours, not `mt5linux`: it exposes only an allow-list of MT5 calls with validated arguments,
  signs every frame, journals every `order_send` by request id so nothing is sent twice, and has its own
  trading gate (`disabled` / `reduce_only` / `enabled`), demo-only switch, account check and volume cap.
  `mt5linux` would also work, but its RPyC "classic" mode lets any local process run arbitrary code in the
  Wine Python, with no authentication and no protection against duplicate orders after a timeout.
* systemd owns every process and restarts it on failure; a watchdog timer checks broker connection, quote
  freshness, bot heartbeat and disk every minute and alerts on Telegram.

## Files

| Path | What it is |
|---|---|
| `install.sh`, `scripts/10..40-*.sh` | Ubuntu installer: hardening, Wine + MT5 + Windows Python, app layout, services |
| `systemd/` | `lebprime-{xvfb,mt5-terminal,mt5-gateway,bot,dashboard,worker}.service`, watchdog and backup timers, on-demand VNC |
| `bin/lebprime-ctl` | operator tool: `status`, `health`, `reconcile`, `gate`, `logs`, `restart-mt5` |
| `bin/lebprime-watchdog` | health check run every minute |
| `bin/lebprime-bootguard` | pauses execution after every reboot or fresh install |
| `bin/lebprime-preflight` | refuses to start the dashboard without strong credentials |
| `bin/lebprime-backup` | restic backups: `run`, `verify`, `restore` |
| `migrate_state.py` | moves data and state from Windows (export → import, both with `--dry-run`) |
| `config/*.example`, `config/*` | env templates (secrets), Caddy, fail2ban, SSH, unattended-upgrades, journald, logrotate |
| `requirements-linux.txt`, `requirements-gateway-wine.txt` | pinned dependencies for each side |

## Layout on the server

| Path | Contents | Owner |
|---|---|---|
| `/opt/lebprime/app` | code (read-only to services) + `config.json` | root, config.json lebprime |
| `/opt/lebprime/venv` | native Python venv | root |
| `/etc/lebprime` | `lebprime.env`, `gateway.env`, `gateway.key`, `backup.env`, `restic.password` | root, per-file groups |
| `/var/lib/lebprime/{storage,reports,backtests,LebPrime_Results}` | data, symlinked into the app folder | lebprime |
| `/var/log/lebprime` | `bot.log`, `errors.log`, CSV journals (symlinked as `app/logs`) | lebprime |
| `/var/lib/lebprime-mt5/wine` | Wine prefix with the terminal and Windows Python | lebprime-mt5 |
| `/var/lib/lebprime-mt5/gateway/executions.jsonl` | durable order-request journal | lebprime-mt5 |

Only the bot service gets the `lebprime-gw` group that can read the gateway key, so the dashboard and the
worker cannot place orders: one execution owner per account.

## Quick start

1. Create the VPS (see "VPS" below) with your SSH public key for a sudo user. Package the code only (no data,
   venvs or `.env`) from the branch you want to deploy, and copy it over. On the laptop, in the project folder:
   ```powershell
   git archive --format=tar.gz -o "$env:USERPROFILE\lebprime-src.tar.gz" deploy/linux-vps
   ```
   ```powershell
   scp "$env:USERPROFILE\lebprime-src.tar.gz" youruser@VPS_IP:~/
   ```
   Then on the VPS: `mkdir ~/lebprime-src && tar xzf ~/lebprime-src.tar.gz -C ~/lebprime-src`.
2. On the VPS:
   ```bash
   cd ~/lebprime-src/deploy/linux
   sudo ADMIN_USER=$USER APP_SOURCE=$HOME/lebprime-src LEBPRIME_DOMAIN=bot.example.com ./install.sh
   ```
   Leave `LEBPRIME_DOMAIN` out to keep the dashboard private and reach it with
   `ssh -L 8501:127.0.0.1:8501 youruser@VPS_IP`, then http://localhost:8501.
3. Fill in `/etc/lebprime/lebprime.env` and `/etc/lebprime/gateway.env` (`sudo -e`).
4. Continue with MIGRATION_PLAN.md, phase 2 (Wine compatibility gate). Do not skip it.

## Secrets

Nothing secret is in git. Real values live only in `/etc/lebprime/*` on the VPS and in your password manager.
The backups deliberately exclude them. `migrate_state.py` copies `config.json` (which has no credentials) and
data, never `.env`.

## VPS

* **Size:** 4 vCPU, 8 GB RAM, 80 GB NVMe, Ubuntu 24.04 LTS, x86-64 (Wine and MT5 need x86; no ARM).
  The bot and MT5 need about 1.5 GB; the rest is headroom for one research job (capped at 2 vCPU and 4 GB).
* **Location:** London. OneRoyal does not publish its server location; VPS sellers list OneRoyal at about 1 ms
  from London LD4, and most MT5 brokers sit in Equinix LD4. Before paying for a long term, open the terminal
  over VNC and read the ping in the bottom-right corner (or `lebprime-ctl health` → `terminal_ping_ms`).
  Under 10 ms is good. The demo and live servers can be in different places, so check again before going live.
* **Providers with London regions:** DigitalOcean (LON1), Vultr (London), Akamai/Linode (London), AWS
  (eu-west-2). Pick one with snapshots, so you can snapshot right after phase 2 passes.
