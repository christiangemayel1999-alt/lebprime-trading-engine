#!/usr/bin/env bash
# App layout, native Python venv, secrets and the gateway key.
#   /opt/lebprime/app      code (root-owned, read-only to services) + config.json (owned by lebprime)
#   /opt/lebprime/venv     native Python virtualenv (pinned requirements-linux.txt)
#   /opt/lebprime/deploy   -> app/deploy/linux
#   /etc/lebprime          secrets and env files (never in git, never in backups)
#   /var/lib/lebprime      storage, reports, backtests, results (symlinked into the app folder)
#   /var/log/lebprime      bot logs (symlinked as app/logs)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${APP_SOURCE:?}"
APP=/opt/lebprime/app
export DEBIAN_FRONTEND=noninteractive
apt-get -y install python3 python3-venv python3-dev build-essential acl openssl

getent group lebprime-gw >/dev/null || groupadd --system lebprime-gw
id lebprime >/dev/null 2>&1 || useradd --system --home-dir /var/lib/lebprime --shell /usr/sbin/nologin lebprime
install -d -m 755 /opt/lebprime
install -d -o lebprime -g lebprime -m 750 /var/lib/lebprime /var/log/lebprime
for d in storage reports backtests LebPrime_Results; do install -d -o lebprime -g lebprime -m 750 "/var/lib/lebprime/$d"; done
install -d -o root -g root -m 750 /var/lib/lebprime-watchdog /var/lib/lebprime-backup

# --- Code: copy from APP_SOURCE without data, virtualenvs, Windows launchers or secrets.
install -d "$APP"
rsync -a --delete \
  --exclude '.git/' --exclude '.venv*/' --exclude 'venv/' --exclude '__pycache__/' --exclude '.pytest*' \
  --exclude '/storage' --exclude '/logs' --exclude '/reports' --exclude '/backtests' --exclude '/LebPrime_Results' \
  --exclude '/LebPrime AI Research/' --exclude '.env' --exclude '.env.*' --exclude '/config.json' --exclude 'config.json.bak*' \
  --exclude '*.bat' --exclude '*.ps1' \
  "$APP_SOURCE/" "$APP/"
chown -R root:root "$APP"
find "$APP" -type d -exec chmod 755 {} +
find "$APP" -type f -exec chmod 644 {} +
chmod 755 "$APP"/deploy/linux/bin/* "$APP"/deploy/linux/*.sh "$APP"/deploy/linux/scripts/*.sh "$APP"/deploy/linux/migrate_state.py
# The app rewrites config.json atomically in its own folder. Sticky + group-writable lets the
# lebprime user replace its own config.json but not delete or replace any root-owned code file.
chown root:lebprime "$APP"
chmod 1775 "$APP"
if [[ ! -f "$APP/config.json" ]]; then
  install -o lebprime -g lebprime -m 640 "$APP_SOURCE/config.json" "$APP/config.json"
fi
for d in storage reports backtests LebPrime_Results; do ln -sfn "/var/lib/lebprime/$d" "$APP/$d"; done
ln -sfn /var/log/lebprime "$APP/logs"
ln -sfn "$APP/deploy/linux" /opt/lebprime/deploy
ln -sfn /opt/lebprime/deploy/bin/lebprime-ctl /usr/local/sbin/lebprime-ctl
ln -sfn /opt/lebprime/deploy/bin/lebprime-backup /usr/local/sbin/lebprime-backup

# --- Native venv (pinned) and bytecode compiled now, because services cannot write into the code tree.
[[ -x /opt/lebprime/venv/bin/python ]] || python3 -m venv /opt/lebprime/venv
/opt/lebprime/venv/bin/pip install --disable-pip-version-check -q --upgrade pip
/opt/lebprime/venv/bin/pip install --disable-pip-version-check -q -r "$APP/deploy/linux/requirements-linux.txt"
/opt/lebprime/venv/bin/python -m compileall -q "$APP" >/dev/null || true

# --- Secrets in /etc/lebprime, readable only by the services that need them.
install -d -m 750 -o root -g lebprime /etc/lebprime
setfacl -m u:lebprime-mt5:x /etc/lebprime
if [[ ! -f /etc/lebprime/gateway.key ]]; then
  ( umask 077; openssl rand -hex 32 > /etc/lebprime/gateway.key )
fi
chown root:lebprime-gw /etc/lebprime/gateway.key
chmod 640 /etc/lebprime/gateway.key
[[ -f /etc/lebprime/lebprime.env ]] || install -o root -g lebprime -m 640 "$HERE/config/lebprime.env.example" /etc/lebprime/lebprime.env
[[ -f /etc/lebprime/gateway.env ]] || install -o root -g lebprime-mt5 -m 640 "$HERE/config/gateway.env.example" /etc/lebprime/gateway.env
[[ -f /etc/lebprime/backup.env ]] || install -o root -g root -m 600 "$HERE/config/backup.env.example" /etc/lebprime/backup.env
[[ -f /etc/lebprime/restic.password ]] || ( umask 077; openssl rand -base64 32 > /etc/lebprime/restic.password )

# Smoke test: the bot's modules import on Linux without the MetaTrader5 package.
sudo -u lebprime env LEBPRIME_MT5_TRANSPORT=auto /opt/lebprime/venv/bin/python -c \
  "import sys; sys.path.insert(0, '$APP'); import mt5_connector, trading_bot.mt5_transport.client; print('imports OK')"

echo "App installed. Fill in /etc/lebprime/lebprime.env, /etc/lebprime/gateway.env and /etc/lebprime/backup.env."
echo "Save /etc/lebprime/restic.password in your password manager: backups cannot be restored without it."
