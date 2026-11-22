#!/usr/bin/env bash
# Read-only self-check of the LebPrime VPS. Changes nothing, prints no secret values.
#   sudo bash /opt/lebprime/deploy/scripts/vps_check.sh            # everything
#   sudo bash /opt/lebprime/deploy/scripts/vps_check.sh --pre-install   # only the bare server (before install.sh)
# Exit code 0 = no FAIL. WARN lines are things to look at, not blockers.
set -uo pipefail
PRE=0; [[ "${1:-}" == "--pre-install" ]] && PRE=1
fails=0; warns=0
pass() { printf '  [PASS] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; warns=$((warns + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; fails=$((fails + 1)); }
is() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else ${3:-fail} "$1"; fi; }
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 2; }

echo "== Server"
. /etc/os-release
[[ "$ID-$VERSION_ID" == "ubuntu-24.04" ]] && pass "Ubuntu 24.04 ($PRETTY_NAME)" || fail "expected Ubuntu 24.04, found $PRETTY_NAME"
[[ "$(uname -m)" == "x86_64" ]] && pass "x86-64 CPU" || fail "CPU is $(uname -m); Wine + MT5 need x86-64"
cpus=$(nproc); mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1048576 + 0.5}' /proc/meminfo); disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
(( cpus >= 4 )) && pass "$cpus vCPU" || warn "$cpus vCPU (plan: 4)"
(( mem_gb >= 7 )) && pass "${mem_gb} GB RAM" || warn "${mem_gb} GB RAM (plan: 8)"
(( disk_gb >= 40 )) && pass "${disk_gb} GB free on /" || warn "only ${disk_gb} GB free on / (data ~3 GB, Wine ~3 GB, backups staging ~3 GB)"
[[ "$(timedatectl show -p Timezone --value)" == "UTC" ]] && pass "timezone UTC" || warn "timezone is $(timedatectl show -p Timezone --value) (install sets UTC)"
[[ "$(timedatectl show -p NTPSynchronized --value)" == "yes" ]] && pass "clock synchronised" || warn "clock not NTP-synchronised yet"
for host in dl.winehq.org www.python.org download.mql5.com pypi.org; do
  is "outbound HTTPS to $host" "curl -fsS -o /dev/null -m 10 https://$host/" warn
done
if (( PRE )); then echo; echo "pre-install: $fails FAIL, $warns WARN"; exit $(( fails > 0 )); fi

echo "== Hardening"
sshd_cfg="$(sshd -T 2>/dev/null)"
grep -qi '^passwordauthentication no' <<<"$sshd_cfg" && pass "SSH passwords disabled" || fail "SSH still accepts passwords"
grep -qi '^permitrootlogin no' <<<"$sshd_cfg" && pass "SSH root login disabled" || fail "SSH root login allowed"
ufw status | grep -q 'Status: active' && pass "firewall active" || fail "ufw firewall not active"
extra=$(ufw status | awk '/ALLOW|LIMIT/ {print $1}' | sed 's/ *(v6)//; s#/tcp##' | sort -u | grep -vxE '22|80|443' | xargs)
[[ -z "$extra" ]] && pass "firewall allows only 22, 80, 443" || warn "firewall also allows: $extra"
is "fail2ban running" "systemctl is-active --quiet fail2ban"
is "automatic security updates on" "systemctl is-active --quiet unattended-upgrades"
is "Wine excluded from automatic updates" "grep -q winehq /etc/apt/apt.conf.d/52lebprime-unattended"
for port in 8501 18812 5900; do
  if ss -ltnH "sport = :$port" | awk '{print $4}' | grep -vqE '^(127\.0\.0\.1|\[::1\]):'; then fail "port $port listens on a public address"
  else pass "port $port local-only (or closed)"; fi
done

echo "== Wine / MT5"
PREFIX=/var/lib/lebprime-mt5/wine
as_mt5() { sudo -u lebprime-mt5 -H env WINEPREFIX="$PREFIX" WINEDEBUG=-all "$@"; }
is "wine installed ($(wine --version 2>/dev/null))" "command -v wine"
is "wine version held" "apt-mark showhold | grep -q winehq-stable" warn
is "Wine prefix exists" "test -f $PREFIX/system.reg"
is "Windows Python installed" "test -f $PREFIX/drive_c/Python312/python.exe"
mt5ver=$(as_mt5 wine 'C:\Python312\python.exe' -c "import MetaTrader5 as m; print(m.__version__)" 2>/dev/null | tr -d '\r')
[[ -n "$mt5ver" ]] && pass "MetaTrader5 package imports in Wine Python ($mt5ver)" || fail "MetaTrader5 does not import in Wine Python"
terminals=$(ls "$PREFIX/drive_c/Program Files/"*/terminal64.exe 2>/dev/null | wc -l)
(( terminals >= 1 )) && pass "MT5 terminal installed ($terminals found)" || fail "no terminal64.exe in the prefix"

echo "== App and secrets"
is "app code at /opt/lebprime/app" "test -f /opt/lebprime/app/main.py"
is "native venv works" "/opt/lebprime/venv/bin/python -c 'import pandas, fastapi'"
for link in storage logs reports backtests; do is "app/$link -> data dir" "test -L /opt/lebprime/app/$link"; done
[[ "$(stat -c '%U:%G %a' /etc/lebprime/gateway.key 2>/dev/null)" == "root:lebprime-gw 640" ]] && pass "gateway key permissions" || fail "gateway key missing or wrong permissions"
check_env() {  # file var...
  local file=$1; shift
  for var in "$@"; do
    if grep -qE "^${var}=.+" "$file" 2>/dev/null; then pass "$(basename "$file"): $var is set"; else fail "$(basename "$file"): $var is empty"; fi
  done
}
check_env /etc/lebprime/lebprime.env MT5_LOGIN MT5_PASSWORD MT5_SERVER DASHBOARD_USERNAME DASHBOARD_PASSWORD
check_env /etc/lebprime/gateway.env LEBPRIME_MT5_EXPECTED_LOGIN LEBPRIME_GATEWAY_SYMBOLS
grep -q '^LEBPRIME_GATEWAY_DEMO_ONLY=1' /etc/lebprime/gateway.env && pass "gateway demo-only" || fail "gateway is NOT demo-only"
gate=$(sed -n 's/^LEBPRIME_GATEWAY_TRADING=//p' /etc/lebprime/gateway.env)
[[ "$gate" == "disabled" ]] && pass "gateway trading gate: disabled" || warn "gateway trading gate: $gate"
grep -q '^TELEGRAM_REMOTE_CONTROL_ENABLED=1' /etc/lebprime/lebprime.env && warn "Telegram polling ON here: make sure the laptop is not also polling" || pass "Telegram polling off (laptop still owns it)"
if git -C /opt/lebprime/app status >/dev/null 2>&1; then warn "/opt/lebprime/app is a git checkout (expected a code copy)"; fi
if grep -rqsE '^(MT5_PASSWORD|DASHBOARD_PASSWORD)=.+' /opt/lebprime/app --include='*.env' --include='.env'; then fail "a .env with passwords exists inside the app folder"; else pass "no .env secrets inside the app folder"; fi

echo "== Services"
for unit in lebprime-xvfb lebprime-mt5-terminal lebprime-mt5-gateway lebprime-dashboard lebprime-worker lebprime-bot lebprime-watchdog.timer; do
  printf '  %-30s enabled=%-9s active=%s\n' "$unit" "$(systemctl is-enabled "$unit" 2>/dev/null)" "$(systemctl is-active "$unit" 2>/dev/null)"
done
if systemctl is-active --quiet lebprime-mt5-gateway; then
  health=$(/opt/lebprime/deploy/bin/lebprime-ctl health 2>/dev/null)
  jq -e '.terminal_connected == true' >/dev/null 2>&1 <<<"$health" && pass "terminal connected to broker (ping $(jq -r .terminal_ping_ms <<<"$health") ms)" || fail "terminal not connected (lebprime-ctl health)"
  jq -e '.account_is_demo == true' >/dev/null 2>&1 <<<"$health" && pass "terminal is on a demo account" || fail "terminal is NOT on a demo account"
  jq -e '.expected_login_ok == true' >/dev/null 2>&1 <<<"$health" && pass "terminal on the expected login" || fail "terminal on an unexpected login"
  amb=$(jq -r '.executions.ambiguous // 0' <<<"$health" 2>/dev/null)
  [[ "$amb" == "0" ]] && pass "no ambiguous executions" || fail "$amb ambiguous executions: reconcile (RUNBOOK)"
else
  warn "gateway not running: broker checks skipped"
fi
cs=/var/lib/lebprime/storage/control_state.json
if [[ -f $cs ]]; then
  jq -e '.execution_paused == true' >/dev/null <"$cs" && pass "execution paused" || warn "execution is NOT paused"
fi

echo
echo "result: $fails FAIL, $warns WARN"
exit $(( fails > 0 ))
