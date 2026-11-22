# VPS deployment checklist

Tick these off in order. Details and reasons are in MIGRATION_PLAN.md, and fixes are in RUNBOOK.md.
**For the whole checklist:** demo account only, execution paused, gateway trading gate `disabled`. Version 1
strategy changes are not deployed until they're approved; the server can be fully prepared and tested without them.

## A. What only you can do (Christian)

- [ ] **A1. Create an SSH key on the laptop** (once). In PowerShell:
  ```powershell
  ssh-keygen -t ed25519 -C "lebprime-vps"
  ```
  Press Enter to accept `C:\Users\gemay\.ssh\id_ed25519`, and set a passphrase. Then show the **public** half:
  ```powershell
  Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
  ```
  The `.pub` file is safe to paste anywhere. Never share `id_ed25519` (no `.pub`).
- [ ] **A2. Buy the VPS:** Ubuntu 24.04 LTS, x86-64, 4 vCPU, 8 GB RAM, 80 GB NVMe, London region, with snapshots
  enabled. Paste the `.pub` key into the provider's "SSH keys" box when creating it. No password login.
- [ ] **A3. Send me in the chat:** the VPS IP address, the login user the provider gave you (often `root` or
  `ubuntu`), and whether you want the dashboard on a domain (e.g. `bot.yourdomain.com`; then add a DNS A record
  pointing to the IP) or private over an SSH tunnel only. The IP and user are fine to share. **Passwords are not.**
- [ ] **A4. Have the OneRoyal demo login ready** (login number, password, server `OneRoyal-Demo`). You will
  type it yourself on the server in step C3 and in the MT5 window in step D2. Don't put it in the chat.
- [ ] **A5. Pick a dashboard username and a password of 16+ characters**, and keep them in your password manager.
- [ ] **A6. (Later, before handover)** A Backblaze B2 or S3 bucket and key for encrypted off-server backups.

## B. Prepare the server (I can guide you through this or run it once I have SSH access)

- [ ] **B1. First login and an admin user** (skip if the provider already gave you a non-root sudo user):
  ```bash
  ssh root@VPS_IP
  adduser christian && usermod -aG sudo christian
  rsync --archive --chown=christian:christian ~/.ssh /home/christian
  exit
  ssh christian@VPS_IP        # must work before you continue
  ```
- [ ] **B2. Copy the code** from the laptop (project folder, PowerShell):
  ```powershell
  git archive --format=tar.gz -o "$env:USERPROFILE\lebprime-src.tar.gz" deploy/linux-vps
  ```
  ```powershell
  scp "$env:USERPROFILE\lebprime-src.tar.gz" christian@VPS_IP:~/
  ```
  On the VPS: `mkdir -p ~/lebprime-src && tar xzf ~/lebprime-src.tar.gz -C ~/lebprime-src`
- [ ] **B3. Bare-server check:** `sudo bash ~/lebprime-src/deploy/linux/scripts/vps_check.sh --pre-install` → no FAIL.
- [ ] **B4. Install** (about 15 to 25 minutes, mostly downloads):
  ```bash
  cd ~/lebprime-src/deploy/linux
  sudo ADMIN_USER=christian APP_SOURCE=$HOME/lebprime-src ./install.sh
  # with a domain: add LEBPRIME_DOMAIN=bot.yourdomain.com before ./install.sh
  ```
- [ ] **B5. Test that SSH still works from a second window** before you close the first one
  (the installer turns off password login and root login).

## C. Secrets (you type these on the server)

- [ ] **C1.** `sudo nano /etc/lebprime/lebprime.env`: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER=OneRoyal-Demo`,
  `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, Telegram token and chat id. Keep `TELEGRAM_REMOTE_CONTROL_ENABLED=0`.
- [ ] **C2.** `sudo nano /etc/lebprime/gateway.env`: `LEBPRIME_MT5_EXPECTED_LOGIN` = the demo login number.
  Leave `LEBPRIME_GATEWAY_TRADING=disabled` and `LEBPRIME_GATEWAY_DEMO_ONLY=1`.
  Optional: the same `MT5_LOGIN`/`MT5_PASSWORD` so the gateway can log in again by itself after a restart.
- [ ] **C3.** Save `/etc/lebprime/restic.password` in your password manager: `sudo cat /etc/lebprime/restic.password`.

## D. Wine/MT5 compatibility gate (go/no-go)

- [ ] **D1.** `sudo systemctl start lebprime-xvfb lebprime-mt5-terminal`
- [ ] **D2. Log into the demo account in MT5 yourself, over VNC through SSH:**
  ```bash
  sudo -u lebprime-mt5 x11vnc -storepasswd /var/lib/lebprime-mt5/.vnc/passwd
  sudo systemctl start lebprime-vnc
  ```
  On the laptop: `ssh -L 5900:127.0.0.1:5900 christian@VPS_IP`, open a VNC viewer (e.g. TightVNC or RealVNC) at
  `localhost:5900`. In MT5: File → Login to Trade Account, enter the demo login, tick "Save password".
  Write down the ping shown bottom right. Then `sudo systemctl stop lebprime-vnc`.
- [ ] **D3.** `sudo systemctl start lebprime-mt5-gateway` then
  `sudo /opt/lebprime/venv/bin/python /opt/lebprime/deploy/scripts/gateway_smoke.py`.
  It checks all four Version 1 symbols (XAUUSD, XAGUSD, US30, USTEC), their contract specs and M1 to H4 candles.
  If a symbol fails, the script lists the broker's spelling: use that name in `LEBPRIME_GATEWAY_SYMBOLS`.
- [ ] **D4.** Set the per-symbol lot caps in `gateway.env` (`LEBPRIME_GATEWAY_MAX_VOLUME`) from the contract sizes
  D3 printed: about twice the largest order 1% risk can produce on your demo balance.
- [ ] **D5. Demo order round trip:** `sudo lebprime-ctl gate enabled`, run the smoke test with `--demo-order`
  (smallest lot on Gold, opened and closed at once), then `sudo lebprime-ctl gate disabled`.
- [ ] **D6. Reboot test:** `sudo reboot`, wait 3 minutes, reconnect, run D3 again **without** VNC.
  The terminal must have logged in by itself.
- [ ] **D7.** `sudo bash /opt/lebprime/deploy/scripts/vps_check.sh` → no FAIL. **Take a provider snapshot now.**

If D1 to D6 can't be made to work, stop: the fallback is a Windows VPS. The bot code works there unchanged.

## E. Later, after Version 1 is approved

- [ ] E1. Update the code on the server (RUNBOOK, "Updating the bot code") with the approved commit.
- [ ] E2. Data transfer and state migration with `migrate_state.py` (plan phases 4 and 5), dry-run first.
- [ ] E3. Progressive activation: research → read-only bot (gate disabled) → demo execution (plan phase 6).
- [ ] E4. Acceptance list (plan phase 7), including 48 market-open hours on demo.
- [ ] E5. Handover: stop the laptop bot and its Telegram polling, then enable Telegram and backups on the VPS.
