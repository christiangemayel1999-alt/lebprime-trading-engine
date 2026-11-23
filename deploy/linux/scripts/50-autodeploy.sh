#!/usr/bin/env bash
# Auto-deploy setup: read-only GitHub deploy key + a timer that deploys `main` when it changes.
#   sudo REPO_SSH_URL=git@github.com:owner/repo.git bash scripts/50-autodeploy.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${REPO_SSH_URL:?set REPO_SSH_URL, e.g. git@github.com:owner/repo.git}"
STATE=/var/lib/lebprime-deploy
# GitHub's published SSH host key fingerprint (docs.github.com, "GitHub's SSH key fingerprints").
GITHUB_ED25519_FP="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

apt-get -y -qq install git >/dev/null
id lebprime-deploy >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --create-home --shell /usr/sbin/nologin lebprime-deploy
install -d -o lebprime-deploy -g lebprime-deploy -m 750 "$STATE" "$STATE/.ssh" "$STATE/staging"
echo "$REPO_SSH_URL" > "$STATE/repo-url"
if [[ ! -f "$STATE/.ssh/id_ed25519" ]]; then
  sudo -u lebprime-deploy ssh-keygen -q -t ed25519 -N "" -C "lebprime-vps-deploy (read-only)" -f "$STATE/.ssh/id_ed25519"
fi
keys=$(ssh-keyscan -t ed25519 github.com 2>/dev/null)
fp=$(ssh-keygen -lf - <<<"$keys" | awk '{print $2}')
if [[ "$fp" != "$GITHUB_ED25519_FP" ]]; then
  echo "github.com host key fingerprint $fp does not match GitHub's published $GITHUB_ED25519_FP; refusing" >&2
  exit 1
fi
echo "$keys" > "$STATE/.ssh/known_hosts"
chown lebprime-deploy:lebprime-deploy "$STATE/.ssh/known_hosts"
install -m 644 "$HERE"/systemd/lebprime-autodeploy.service "$HERE"/systemd/lebprime-autodeploy.timer /etc/systemd/system/
ln -sfn /opt/lebprime/deploy/bin/lebprime-autodeploy /usr/local/sbin/lebprime-autodeploy
systemctl daemon-reload
systemctl enable --now lebprime-autodeploy.timer
echo
echo "Add this key in GitHub: repository > Settings > Deploy keys > Add deploy key (leave 'Allow write access' OFF):"
echo
cat "$STATE/.ssh/id_ed25519.pub"
echo
echo "The server then deploys every new commit on main within about 2 minutes. Status: sudo lebprime-autodeploy status"
