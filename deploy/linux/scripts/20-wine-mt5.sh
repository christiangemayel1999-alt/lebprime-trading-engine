#!/usr/bin/env bash
# Wine + virtual display + MT5 terminal + Windows Python with MetaTrader5, owned by the lebprime-mt5 user.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_VERSION="${WINE_PYTHON_VERSION:-3.12.10}"
PY_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe"
MT5_URL="${MT5_SETUP_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
HOME_MT5=/var/lib/lebprime-mt5
PREFIX="$HOME_MT5/wine"
DL="$HOME_MT5/downloads"
export DEBIAN_FRONTEND=noninteractive

# --- WineHQ stable for Ubuntu 24.04 (noble).
dpkg --add-architecture i386
install -d -m 755 /etc/apt/keyrings
curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor --yes -o /etc/apt/keyrings/winehq-archive.key
curl -fsSL -o /etc/apt/sources.list.d/winehq-noble.sources https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources
apt-get update
apt-get -y install --install-recommends winehq-stable
apt-get -y install xvfb x11vnc xauth cabextract winbind fonts-dejavu-core
# Hold Wine at the tested version; upgrade by hand (RUNBOOK.md, "Upgrading Wine").
apt-mark hold winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 >/dev/null 2>&1 || true

# --- Dedicated non-root user that owns the Wine prefix, the terminal and the gateway.
getent group lebprime-gw >/dev/null || groupadd --system lebprime-gw
id lebprime-mt5 >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_MT5" --shell /usr/sbin/nologin lebprime-mt5
install -d -o lebprime-mt5 -g lebprime-mt5 -m 750 "$HOME_MT5" "$HOME_MT5/gateway" "$DL" "$HOME_MT5/.vnc"

as_mt5() { sudo -u lebprime-mt5 -H env WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEARCH=win64 TZ=UTC "$@"; }

if [[ ! -f "$PREFIX/system.reg" ]]; then
  as_mt5 xvfb-run -a wineboot --init
  as_mt5 xvfb-run -a winecfg -v win10 || true
  as_mt5 wineserver -w
fi

download() {  # url dest [expected-sha256]
  local url="$1" dest="$2" sum="${3:-}"
  [[ -f "$dest" ]] || sudo -u lebprime-mt5 curl -fL --retry 4 -o "$dest" "$url"
  echo "sha256 $(sha256sum "$dest" | cut -d' ' -f1)  $(basename "$dest")"
  if [[ -n "$sum" ]]; then echo "$sum  $dest" | sha256sum -c -; fi
}

# --- Windows Python inside the prefix at C:\Python312 (python.org signed installer, HTTPS).
if [[ ! -f "$PREFIX/drive_c/Python312/python.exe" ]]; then
  download "$PY_URL" "$DL/python-installer.exe" "${WINE_PYTHON_SHA256:-}"
  as_mt5 xvfb-run -a wine "$DL/python-installer.exe" /quiet InstallAllUsers=0 'TargetDir=C:\Python312' PrependPath=0 Include_test=0 Include_launcher=0 Include_tcltk=0
  as_mt5 wineserver -w
fi
install -o lebprime-mt5 -g lebprime-mt5 -m 644 "$HERE/requirements-gateway-wine.txt" "$DL/requirements-gateway-wine.txt"
as_mt5 wine 'C:\Python312\python.exe' -m pip install --disable-pip-version-check --no-warn-script-location -r 'Z:\var\lib\lebprime-mt5\downloads\requirements-gateway-wine.txt'
as_mt5 wine 'C:\Python312\python.exe' -c "import MetaTrader5, numpy; print('MetaTrader5', MetaTrader5.__version__, 'numpy', numpy.__version__)"

# --- MT5 terminal (official MetaQuotes installer; set MT5_SETUP_URL for a broker-branded one).
if ! ls "$PREFIX/drive_c/Program Files/"*/terminal64.exe >/dev/null 2>&1; then
  download "$MT5_URL" "$DL/mt5setup.exe" "${MT5_SETUP_SHA256:-}"
  as_mt5 xvfb-run -a wine "$DL/mt5setup.exe" /auto
  as_mt5 wineserver -w || true
fi
echo "MT5 terminals found:"
ls -1 "$PREFIX/drive_c/Program Files/"*/terminal64.exe 2>/dev/null || echo "  none yet: install it over VNC (RUNBOOK.md, 'Looking at the MT5 screen')"
echo "Wine/MT5 step done. The first broker login is done by hand over VNC (MIGRATION_PLAN.md, phase 2)."
