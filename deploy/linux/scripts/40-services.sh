#!/usr/bin/env bash
# systemd units, Caddy (HTTPS reverse proxy), restic, journald, logrotate, fail2ban dashboard jail.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

# --- Caddy from its official repository.
if ! command -v caddy >/dev/null; then
  apt-get -y install debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get -y install caddy
fi
apt-get -y install restic
install -d -o caddy -g caddy -m 750 /var/log/caddy
touch /var/log/caddy/lebprime-access.log
chown caddy:caddy /var/log/caddy/lebprime-access.log
install -m 644 "$HERE/config/Caddyfile" /etc/caddy/Caddyfile
if [[ -n "${LEBPRIME_DOMAIN:-}" ]]; then
  touch /etc/default/caddy
  sed -i '/^LEBPRIME_DOMAIN=/d;/^LEBPRIME_ACME_EMAIL=/d' /etc/default/caddy
  printf 'LEBPRIME_DOMAIN=%s\nLEBPRIME_ACME_EMAIL=%s\n' "$LEBPRIME_DOMAIN" "${LEBPRIME_ACME_EMAIL:-}" >> /etc/default/caddy
  install -d /etc/systemd/system/caddy.service.d
  printf '[Service]\nEnvironmentFile=/etc/default/caddy\n' > /etc/systemd/system/caddy.service.d/lebprime.conf
else
  echo "LEBPRIME_DOMAIN not set: Caddy is installed but stopped; reach the dashboard through an SSH tunnel (README)."
  systemctl disable --now caddy >/dev/null 2>&1 || true
fi
install -m 644 "$HERE/config/fail2ban-lebprime.conf" /etc/fail2ban/jail.d/lebprime.conf
rm -f /etc/fail2ban/jail.d/sshd-lebprime.conf
systemctl restart fail2ban

# --- Logs.
install -d /etc/systemd/journald.conf.d
install -m 644 "$HERE/config/journald-lebprime.conf" /etc/systemd/journald.conf.d/lebprime.conf
systemctl restart systemd-journald
install -m 644 "$HERE/config/logrotate-lebprime" /etc/logrotate.d/lebprime
install -m 644 "$HERE/config/tmpfiles-lebprime.conf" /etc/tmpfiles.d/lebprime.conf
systemd-tmpfiles --create /etc/tmpfiles.d/lebprime.conf

# --- systemd units.
install -m 644 "$HERE"/systemd/*.service "$HERE"/systemd/*.timer "$HERE"/systemd/lebprime.target /etc/systemd/system/
systemctl daemon-reload
# Safe to enable now: display, terminal, gateway (trading gate disabled), dashboard, worker, watchdog.
systemctl enable lebprime.target lebprime-xvfb lebprime-mt5-terminal lebprime-mt5-gateway lebprime-dashboard lebprime-worker lebprime-watchdog.timer
# Enabled later, on purpose: lebprime-bot (phase 6b) and lebprime-backup.timer (once backup.env is filled in).
systemctl disable lebprime-bot >/dev/null 2>&1 || true
if [[ -n "${LEBPRIME_DOMAIN:-}" ]]; then
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile --envfile /etc/default/caddy
  systemctl enable caddy
  systemctl restart caddy
fi
echo "Services installed but not started (except Caddy). Continue with MIGRATION_PLAN.md."
