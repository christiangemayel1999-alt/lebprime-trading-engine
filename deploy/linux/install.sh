#!/usr/bin/env bash
# LebPrime VPS installer for a fresh Ubuntu 24.04 LTS server. Run as root:
#
#   sudo ADMIN_USER=christian LEBPRIME_DOMAIN=bot.example.com LEBPRIME_ACME_EMAIL=you@example.com \
#        APP_SOURCE=/home/christian/lebprime-src ./install.sh
#
# Steps (each script is idempotent and can be re-run on its own):
#   scripts/10-harden.sh     updates, SSH keys only, firewall, fail2ban, unattended security updates, UTC
#   scripts/20-wine-mt5.sh   Wine, Xvfb, dedicated lebprime-mt5 user, MT5 terminal, Windows Python + MetaTrader5
#   scripts/30-app.sh        lebprime user, /opt /etc /var/lib layout, venv, gateway key, env templates
#   scripts/40-services.sh   systemd units, Caddy (HTTPS), restic, journald/logrotate config
#
# Nothing here starts trading: the gateway gate is "disabled", the gateway is demo-only, and the
# bot service is installed but not enabled. Follow MIGRATION_PLAN.md for activation.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
: "${ADMIN_USER:?set ADMIN_USER to your SSH login user (it must already have ~/.ssh/authorized_keys)}"
: "${APP_SOURCE:?set APP_SOURCE to a copy of the repository on this server}"
. /etc/os-release
if [[ "${ID}-${VERSION_ID}" != "ubuntu-24.04" ]]; then
  echo "warning: tested for Ubuntu 24.04, this is ${PRETTY_NAME}" >&2
fi
export ADMIN_USER APP_SOURCE
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
export LEBPRIME_DOMAIN="${LEBPRIME_DOMAIN:-}" LEBPRIME_ACME_EMAIL="${LEBPRIME_ACME_EMAIL:-}" ADMIN_SSH_CIDR="${ADMIN_SSH_CIDR:-}"
for step in 10-harden 20-wine-mt5 30-app 40-services; do
  echo "==================== $step ===================="
  bash "$HERE/scripts/$step.sh"
done
echo
echo "Install finished. Next: MIGRATION_PLAN.md, phase 2 'Wine compatibility gate'."
