#!/usr/bin/env bash
# A restricted `research` SSH account: the laptop key may only run commands inside the research sandbox.
#   sudo PUBKEY_FILE=/home/christian/.ssh/authorized_keys bash scripts/55-research-ssh.sh
set -euo pipefail
PUBKEY_FILE="${PUBKEY_FILE:?file with the public key(s) to authorise, one per line}"
id research >/dev/null 2>&1 || useradd --create-home --shell /bin/sh research
install -d -o research -g research -m 700 /home/research/.ssh
ln -sfn /opt/lebprime/deploy/bin/lebprime-research-ssh /usr/local/sbin/lebprime-research-ssh
ln -sfn /opt/lebprime/deploy/bin/lebprime-research /usr/local/sbin/lebprime-research
: > /home/research/.ssh/authorized_keys
grep -E '^(ssh-ed25519|ssh-rsa|ecdsa-)' "$PUBKEY_FILE" | while read -r line; do
  echo "command=\"/usr/local/sbin/lebprime-research-ssh\",restrict $line" >> /home/research/.ssh/authorized_keys
done
chown research:research /home/research/.ssh/authorized_keys && chmod 600 /home/research/.ssh/authorized_keys
echo 'research ALL=(root) NOPASSWD: /usr/local/sbin/lebprime-research' > /etc/sudoers.d/91-research
chmod 440 /etc/sudoers.d/91-research && visudo -cf /etc/sudoers.d/91-research
conf=/etc/ssh/sshd_config.d/10-lebprime.conf
grep -qE '^AllowUsers .*\bresearch\b' "$conf" || sed -i -E 's/^(AllowUsers .*)$/\1 research/' "$conf"
sshd -t && systemctl reload ssh
echo "research account ready: ssh research@<vps> 'python scripts/<script>.py ...'"
