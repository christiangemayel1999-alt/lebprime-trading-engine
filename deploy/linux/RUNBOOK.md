# Recovery runbook (LebPrime VPS)

All commands run on the VPS over SSH. `lebprime-ctl` and `lebprime-backup` need `sudo`.

## First look, whatever happened

```bash
sudo lebprime-ctl status          # every service + gateway health
sudo lebprime-ctl reconcile       # broker positions/orders vs. the gateway execution journal
journalctl -u lebprime-watchdog --since -1h   # what the watchdog saw and did
sudo lebprime-ctl logs            # follow bot + gateway logs (Ctrl+C to stop)
```

If anything about open positions looks wrong, pause execution from the dashboard first, then investigate.
Pausing is always safe; resuming is a decision.

## Golden rules

1. One machine trades the account. Never run the Windows bot and the VPS bot at the same time on one account.
2. After any reboot, crash loop, restore or "ambiguous execution" alert: reconcile, then resume by hand.
3. Never resend an order whose outcome is unknown. Look at the broker first.

## Incidents

### "order request(s) have an unknown outcome" (ambiguous execution)
The gateway died between sending an order and recording the result. The order may or may not exist.
1. Pause execution in the dashboard.
2. `sudo lebprime-ctl reconcile`: look for a `[BOT]` position or order that the dashboard/journal does not know.
3. If one exists, decide by hand: keep it (the bot's lifecycle manager may adopt it on restart; check the logs)
   or close it in the terminal over VNC. If none exists, nothing was filled.
4. Resume only when broker, dashboard and journal agree. The ambiguous id stays in
   `/var/lib/lebprime-mt5/gateway/executions.jsonl` and is never sent again.

### Gateway or terminal down / broker disconnected
The watchdog restarts the terminal after 3 failed health checks or 5 minutes disconnected (at most every
10 minutes) and alerts. If it keeps failing:
```bash
sudo systemctl status lebprime-mt5-terminal lebprime-mt5-gateway
journalctl -u lebprime-mt5-gateway -n 100
sudo lebprime-ctl restart-mt5
```
Still failing: open the screen over VNC (below). Typical causes: broker password changed, terminal update
dialog waiting for a click, "Invalid account" after the broker reset the demo (demo accounts expire; create
a new one and update `LEBPRIME_MT5_EXPECTED_LOGIN` and the credentials).

### "terminal is logged into X, not the expected account"
Execution is blocked at the gateway on purpose. Log the terminal back into the right account over VNC, or
fix `LEBPRIME_MT5_EXPECTED_LOGIN` in `/etc/lebprime/gateway.env` if the account really changed, then
`sudo systemctl restart lebprime-mt5-gateway`.

### Quotes frozen while the market is open
The watchdog alerts and restarts the terminal (at most every 30 minutes). If quotes are frozen in the terminal
itself (VNC), it is usually the broker side or a holiday session. Check the broker's trading hours before
assuming a fault.

### Bot heartbeat stale / bot crash loop
```bash
journalctl -u lebprime-bot -n 200
tail -n 100 /var/log/lebprime/errors.log
```
systemd gives up after 8 failed starts in 15 minutes. Fix the cause, then
`sudo systemctl reset-failed lebprime-bot && sudo systemctl start lebprime-bot`.

### Dashboard won't start
`journalctl -u lebprime-dashboard -n 50`. "lebprime-preflight: DASHBOARD_PASSWORD must be at least 16
characters" means the credentials in `/etc/lebprime/lebprime.env` are missing or weak, which is intended.

### Locked out of the dashboard (fail2ban)
`sudo fail2ban-client status lebprime-dashboard`, then `sudo fail2ban-client set lebprime-dashboard unbanip YOUR_IP`.

### Disk almost full
`sudo du -xh /var/lib /var/log | sort -h | tail -20`. Usual suspects: `storage/indicator_datasets`,
old reports, journald (capped at 1 GB). Never delete `executions.jsonl` or `bot.db`.

## Reboots and updates

* Security updates install automatically; Wine is excluded and the server never reboots by itself.
  The watchdog alerts when a reboot is needed.
* Reboot on the weekend when the market is closed: `sudo reboot`. Everything starts by itself, the terminal
  logs in by itself, and **execution comes back paused** (boot guard). Reconcile, then resume.

### Upgrading Wine
A Wine upgrade can break the terminal, so it is pinned. On a weekend: take a provider snapshot, then
`sudo apt-mark unhold winehq-stable wine-stable wine-stable-amd64 wine-stable-i386`,
`sudo apt-get install --only-upgrade winehq-stable`, hold again, reboot, and re-run phase 2 of the plan.
If it fails, restore the snapshot.

### Updating the bot code
```bash
# on the laptop: git archive ... (README step 1), scp it over, unpack to ~/lebprime-src-new
sudo systemctl stop lebprime-bot lebprime-dashboard lebprime-worker
sudo APP_SOURCE=$HOME/lebprime-src-new bash /opt/lebprime/deploy/scripts/30-app.sh
sudo bash /opt/lebprime/deploy/scripts/40-services.sh
sudo systemctl start lebprime-dashboard lebprime-worker lebprime-bot
```
`30-app.sh` keeps `config.json`, `/etc/lebprime` and all data. It replaces code only.

## Looking at the MT5 screen (VNC over SSH)

The virtual display is never exposed; VNC listens on localhost only.
```bash
sudo -u lebprime-mt5 x11vnc -storepasswd /var/lib/lebprime-mt5/.vnc/passwd   # once
sudo systemctl start lebprime-vnc
```
On the laptop: `ssh -L 5900:127.0.0.1:5900 youruser@VPS_IP`, then point a VNC viewer at `localhost:5900`.
When done: `sudo systemctl stop lebprime-vnc`.

## Restore from backup

```bash
sudo systemctl stop lebprime-bot lebprime-dashboard lebprime-worker
sudo lebprime-backup restore /tmp/restore            # or: restore /tmp/restore <snapshot-id>
# list snapshots: sudo bash -c 'set -a; . /etc/lebprime/backup.env; restic snapshots'
cd /tmp/restore/var/lib/lebprime-backup/staging && sha256sum --quiet -c SHA256SUMS
sudo rsync -a state/ /var/lib/lebprime/
sudo cp app/config.json /opt/lebprime/app/config.json
sudo chown -R lebprime:lebprime /var/lib/lebprime /opt/lebprime/app/config.json
sudo rm -f /var/lib/lebprime/storage/*.lock /run/lebprime/boot-reconciled   # next start is paused
sudo systemctl start lebprime-dashboard lebprime-worker lebprime-bot
```
Do not restore `gateway/executions.jsonl` over a newer one; it only matters for request ids since the backup.
For a whole new server: run `install.sh`, put back `/etc/lebprime/*` from your password manager, redo the MT5
login (phase 2), then the steps above.

## Two machines may have traded (rollback or handover mistakes)

Stop both bots. Compare `lebprime-ctl reconcile` (broker truth) with both journals. Close duplicates in the
terminal by hand. Start only one bot, paused, and resume after the broker and its journal agree.

## Rotating the gateway key

```bash
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/lebprime/gateway.key'
sudo chown root:lebprime-gw /etc/lebprime/gateway.key && sudo chmod 640 /etc/lebprime/gateway.key
sudo systemctl restart lebprime-mt5-gateway lebprime-bot
```
