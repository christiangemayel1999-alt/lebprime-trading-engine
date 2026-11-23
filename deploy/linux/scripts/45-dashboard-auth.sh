#!/usr/bin/env bash
# Public dashboard: Caddy HTTPS + login gate with username, password and TOTP 2FA.
#   sudo LEBPRIME_DOMAIN=bot.example.com DASH_USER=christian bash scripts/45-dashboard-auth.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${LEBPRIME_DOMAIN:?set LEBPRIME_DOMAIN (DNS A record must point here)}"
DASH_USER="${DASH_USER:-christian}"
export DEBIAN_FRONTEND=noninteractive
apt-get -y -qq install qrencode >/dev/null

id lebprime-auth >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lebprime-auth
setfacl -m u:lebprime-auth:x /etc/lebprime
if [[ ! -f /etc/lebprime/auth.json ]]; then
  python3 /opt/lebprime/deploy/auth/lebprime_auth.py init --username "$DASH_USER"
fi
chown root:lebprime-auth /etc/lebprime/auth.json && chmod 640 /etc/lebprime/auth.json
chmod 600 /etc/lebprime/auth-initial.txt 2>/dev/null || true
ln -sfn /opt/lebprime/deploy/auth/lebprime_auth.py /usr/local/sbin/lebprime-auth
chmod 755 /opt/lebprime/deploy/auth/lebprime_auth.py

# The app's own Basic credentials (from lebprime.env) are injected by Caddy after the 2FA gate.
set -a; . /etc/lebprime/lebprime.env; set +a
: "${DASHBOARD_USERNAME:?DASHBOARD_USERNAME missing in /etc/lebprime/lebprime.env}"
: "${DASHBOARD_PASSWORD:?DASHBOARD_PASSWORD missing in /etc/lebprime/lebprime.env}"
basic=$(printf '%s:%s' "$DASHBOARD_USERNAME" "$DASHBOARD_PASSWORD" | base64 -w0)
umask 077
printf 'LEBPRIME_DOMAIN=%s\nLEBPRIME_DASH_BASIC=%s\n' "$LEBPRIME_DOMAIN" "$basic" > /etc/default/caddy
chmod 600 /etc/default/caddy
umask 022
install -d /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/default/caddy\n' > /etc/systemd/system/caddy.service.d/lebprime.conf
install -m 644 "$HERE/config/Caddyfile" /etc/caddy/Caddyfile
install -m 644 "$HERE/systemd/lebprime-auth.service" /etc/systemd/system/
install -m 644 "$HERE/config/fail2ban-filter-lebprime-auth.conf" /etc/fail2ban/filter.d/lebprime-auth.conf
install -m 644 "$HERE/config/fail2ban-lebprime.conf" /etc/fail2ban/jail.d/lebprime.conf
systemctl daemon-reload
systemctl enable --now lebprime-auth
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile --envfile /etc/default/caddy
systemctl enable caddy
systemctl restart caddy
systemctl restart fail2ban
echo "Dashboard gate ready at https://$LEBPRIME_DOMAIN . Credentials: sudo lebprime-auth show (over SSH)."
