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
# Wine build: MT5's anti-tamper check reports "A debugger has been found running in your system" on
# Wine 10.x and 11.x stable (WineHQ bug 57950). What works (tested 2026-11-23 on Ubuntu 24.04, MT5
# build of that date): Wine staging 11.22 + no AeDebug debugger + Windows 11 mode (set below).
WINE_PACKAGE="${WINE_PACKAGE:-winehq-staging}"
WINE_VERSION="${WINE_VERSION:-11.22~noble-1}"
apt-mark unhold winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 winehq-staging wine-staging wine-staging-amd64 wine-staging-i386 >/dev/null 2>&1 || true
apt-get -y remove winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 >/dev/null 2>&1 || true
flavour="${WINE_PACKAGE#winehq-}"
apt-get -y install --install-recommends --allow-downgrades   "$WINE_PACKAGE=$WINE_VERSION" "wine-$flavour=$WINE_VERSION" "wine-$flavour-amd64=$WINE_VERSION" "wine-$flavour-i386:i386=$WINE_VERSION"
apt-get -y install xvfb x11vnc xauth cabextract winbind fonts-dejavu-core
# Hold Wine at the tested version; upgrade by hand (RUNBOOK.md, "Upgrading Wine").
apt-mark hold "$WINE_PACKAGE" "wine-$flavour" "wine-$flavour-amd64" "wine-$flavour-i386" >/dev/null 2>&1 || true

# --- Dedicated non-root user that owns the Wine prefix, the terminal and the gateway.
getent group lebprime-gw >/dev/null || groupadd --system lebprime-gw
id lebprime-mt5 >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_MT5" --shell /usr/sbin/nologin lebprime-mt5
# Terminal and gateway share one wineserver, so the MT5 user itself must be able to read the gateway key.
usermod -aG lebprime-gw lebprime-mt5
install -d -o lebprime-mt5 -g lebprime-mt5 -m 750 "$HOME_MT5" "$HOME_MT5/gateway" "$DL" "$HOME_MT5/.vnc"

# No Mono/Gecko install prompts (they would wait forever on the invisible display); MT5 needs neither.
# Every Wine step is time-limited so a stuck dialog fails the install instead of hanging it.
as_mt5() { sudo -u lebprime-mt5 -H env WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEARCH=win64 TZ=UTC WINEDLLOVERRIDES="mscoree=d;mshtml=d" timeout "${WINE_STEP_TIMEOUT:-1200}" "$@"; }

# A prefix created by another Wine version is rebuilt (RESET_PREFIX=1 forces it).# Wait for Wine to go idle, but don't hang on background services that never exit (seen with Wine 10).
wine_settle() { sudo -u lebprime-mt5 -H env WINEPREFIX="$PREFIX" sh -c 'timeout 90 wineserver -w || wineserver -k; true'; }

if [[ -f "$PREFIX/.wine-version" && "$(cat "$PREFIX/.wine-version")" != "$(wine --version)" || "${RESET_PREFIX:-0}" == "1" ]]; then
  echo "rebuilding Wine prefix for $(wine --version)"
  pkill -u lebprime-mt5 -f wine || true
  rm -rf "$PREFIX"
fi
if [[ -f "$PREFIX/system.reg" && ! -f "$PREFIX/.wine-version" ]]; then
  echo "prefix has no version stamp: rebuilding it"; pkill -u lebprime-mt5 -f wine || true; rm -rf "$PREFIX"
fi
if [[ ! -f "$PREFIX/system.reg" ]]; then
  as_mt5 xvfb-run -a wineboot --init
  as_mt5 xvfb-run -a winecfg -v win11 || true
  wine_settle
  wine --version | sudo -u lebprime-mt5 tee "$PREFIX/.wine-version" >/dev/null
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
  wine_settle
fi
install -o lebprime-mt5 -g lebprime-mt5 -m 644 "$HERE/requirements-gateway-wine.txt" "$DL/requirements-gateway-wine.txt"
as_mt5 wine 'C:\Python312\python.exe' -m pip install --disable-pip-version-check --no-warn-script-location -r 'Z:\var\lib\lebprime-mt5\downloads\requirements-gateway-wine.txt'
as_mt5 wine 'C:\Python312\python.exe' -c "import MetaTrader5, numpy; print('MetaTrader5', MetaTrader5.__version__, 'numpy', numpy.__version__)"

# --- MT5 terminal (official MetaQuotes installer; set MT5_SETUP_URL for a broker-branded one).
if ! ls "$PREFIX/drive_c/Program Files/"*/terminal64.exe >/dev/null 2>&1; then
  download "$MT5_URL" "$DL/mt5setup.exe" "${MT5_SETUP_SHA256:-}"
  as_mt5 xvfb-run -a wine "$DL/mt5setup.exe" /auto
  wine_settle
fi
# MT5 compatibility: no auto-debugger registered (its presence trips MT5's debugger check).
for key in 'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' 'HKLM\Software\Wow6432Node\Microsoft\Windows NT\CurrentVersion\AeDebug'; do
  as_mt5 wine reg delete "$key" /v Debugger /f >/dev/null 2>&1 || true
done
as_mt5 wine winecfg -v win11 >/dev/null 2>&1 || true
wine_settle
# Allow algorithmic trading (the Python API's order_send needs it); DLL imports stay off.
for ini in "$PREFIX/drive_c/Program Files/"*/Config/common.ini; do
  [[ -f "$ini" ]] || continue
  if ! iconv -f UTF-16LE -t UTF-8 "$ini" | grep -q '^\[Experts\]'; then
    { iconv -f UTF-16LE -t UTF-8 "$ini" | sed 's/^\xEF\xBB\xBF//' | tr -d '\r'
      printf '\n[Experts]\nAllowLiveTrading=1\nAllowDllImport=0\nEnabled=1\nAccount=0\nProfile=0\n'
    } | sed 's/$/\r/' | iconv -f UTF-8 -t UTF-16LE | { printf '\xff\xfe'; cat; } > "$ini.new"
    mv "$ini.new" "$ini"; chown lebprime-mt5:lebprime-mt5 "$ini"
  fi
done
echo "MT5 terminals found:"
ls -1 "$PREFIX/drive_c/Program Files/"*/terminal64.exe 2>/dev/null || echo "  none yet: install it over VNC (RUNBOOK.md, 'Looking at the MT5 screen')"
echo "Wine/MT5 step done. For a broker the fresh terminal doesn't know (e.g. OneRoyal-Demo), copy servers.dat"
echo "from a terminal that knows it into .../MetaTrader 5/Config/ (RUNBOOK.md, 'Broker server list'); the gateway then logs in by itself."
