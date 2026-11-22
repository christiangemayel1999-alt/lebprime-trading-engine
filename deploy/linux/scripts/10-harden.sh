#!/usr/bin/env bash
# Base hardening: security updates, SSH keys only, firewall, fail2ban, UTC clock.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${ADMIN_USER:?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get -y install ufw fail2ban unattended-upgrades apt-listchanges ca-certificates curl gnupg rsync jq sqlite3 chrony acl openssl

timedatectl set-timezone UTC
systemctl enable --now chrony

# --- SSH: refuse to lock the admin out.
admin_home="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
if [[ -z "$admin_home" || ! -s "$admin_home/.ssh/authorized_keys" ]]; then
  echo "ERROR: $ADMIN_USER has no ~/.ssh/authorized_keys. Add your public key first (ssh-copy-id), then re-run." >&2
  exit 1
fi
sed "s/__ADMIN_USER__/$ADMIN_USER/" "$HERE/config/sshd-lebprime.conf" > /etc/ssh/sshd_config.d/10-lebprime.conf
chmod 644 /etc/ssh/sshd_config.d/10-lebprime.conf
sshd -t
systemctl reload ssh || systemctl reload sshd

# --- Firewall: SSH (rate-limited, or only from ADMIN_SSH_CIDR), HTTP for the HTTPS redirect, HTTPS.
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
if [[ -n "${ADMIN_SSH_CIDR:-}" ]]; then
  ufw allow from "$ADMIN_SSH_CIDR" to any port 22 proto tcp
else
  ufw limit 22/tcp
fi
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- fail2ban: SSH jail now, the dashboard jail is added by 40-services.sh once Caddy is installed.
install -m 644 "$HERE/config/fail2ban-filter-lebprime-dashboard.conf" /etc/fail2ban/filter.d/lebprime-dashboard.conf
printf '[sshd]\nenabled  = true\nmaxretry = 4\nfindtime = 10m\nbantime  = 1h\n' > /etc/fail2ban/jail.d/sshd-lebprime.conf
systemctl enable fail2ban
systemctl restart fail2ban

# --- Automatic security updates (never Wine, never an automatic reboot).
install -m 644 "$HERE/config/unattended-upgrades-lebprime.conf" /etc/apt/apt.conf.d/52lebprime-unattended
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::AutocleanInterval "7";\n' > /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now unattended-upgrades

# --- Kernel/network basics.
printf '%s\n' \
  'net.ipv4.conf.all.rp_filter = 1' \
  'net.ipv4.conf.all.accept_redirects = 0' \
  'net.ipv6.conf.all.accept_redirects = 0' \
  'net.ipv4.conf.all.send_redirects = 0' \
  'net.ipv4.tcp_syncookies = 1' \
  'kernel.kptr_restrict = 2' > /etc/sysctl.d/90-lebprime.conf
sysctl --system >/dev/null
echo "hardening done"
