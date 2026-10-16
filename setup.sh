#!/usr/bin/env bash
#
# setup.sh — bootstrap the Even Realities G1 developer backend.
#
# Creates a local virtualenv, installs dependencies, and runs a preflight
# check against the host Bluetooth stack. Safe to re-run.
#
#   ./setup.sh
#
set -euo pipefail

VENV_DIR=".venv"
PY_BIN=""

info()  { printf '\033[0;36m[setup]\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m[ ok ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m[warn]\033[0m %s\n' "$*"; }
err()   { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; }

cd "$(dirname "$0")"

# ---------------------------------------------------------------- platform --
UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME_S" in
    Darwin)                      PLATFORM="macos"   ;;
    Linux)                       PLATFORM="linux"   ;;
    MINGW*|MSYS*|CYGWIN*)        PLATFORM="windows" ;;
    *)                           PLATFORM="unknown" ;;
esac

info "host platform: ${UNAME_S} (${PLATFORM})"

# ------------------------------------------------------------------ python --
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
            PY_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PY_BIN" ]; then
    err "no suitable Python found. bleak 3.x requires Python >= 3.9."
    case "$PLATFORM" in
        macos)   err "install with: brew install python@3.12" ;;
        windows) err "install from https://www.python.org/downloads/ (tick 'Add to PATH')" ;;
        linux)   err "install with: sudo apt install python3-venv python3-pip" ;;
    esac
    exit 1
fi

ok "python: $("$PY_BIN" --version 2>&1) ($(command -v "$PY_BIN"))"

# ------------------------------------------------------------- bluez hints --
if [ "$PLATFORM" = "linux" ]; then
    if ! command -v bluetoothctl >/dev/null 2>&1; then
        warn "bluetoothctl not found — bleak will not be able to reach an adapter."
        warn "install BlueZ first:  sudo apt install bluez libdbus-1-dev"
    else
        ok "bluez: $(bluetoothctl --version 2>&1 | head -1)"
    fi

    if ! systemctl is-active --quiet bluetooth 2>/dev/null; then
        warn "the 'bluetooth' service does not look active."
        warn "try: sudo systemctl enable --now bluetooth"
    fi

    if ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx bluetooth; then
        warn "current user is not in the 'bluetooth' group — scanning will likely fail."
        warn "fix with: sudo usermod -aG bluetooth \"\$USER\"   (then log out and back in)"
    else
        ok "user is a member of the 'bluetooth' group"
    fi
fi

if [ "$PLATFORM" = "windows" ]; then
    warn "Running under MSYS/Git-Bash. bleak needs the WinRT backend, which is"
    warn "only importable from native CPython. Use PowerShell instead:"
    warn "    py -m venv $VENV_DIR; .\\$VENV_DIR\\Scripts\\python -m pip install -r requirements.txt"
fi

# ------------------------------------------------------------------ venv ---
if [ ! -d "$VENV_DIR" ]; then
    info "creating virtualenv in ./$VENV_DIR"
    "$PY_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV_DIR/Scripts/python.exe"

info "installing dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt

ok "installed: $("$VENV_PY" -m pip show bleak | awk '/^Version:/ {print "bleak " $2}')"

# -------------------------------------------------------------- preflight --
info "running bleak import preflight"
"$VENV_PY" - <<'PY'
import sys
try:
    from bleak import BleakClient, BleakScanner
except Exception as exc:  # pragma: no cover - environment dependent
    print(f"[fail] bleak import failed: {exc}", file=sys.stderr)
    raise SystemExit(1)

from bleak import __name__ as _n
try:
    from importlib.metadata import version
    print(f"[ ok ] bleak {version('bleak')} imported cleanly")
except Exception:
    print("[ ok ] bleak imported cleanly")

# Report which backend bleak will actually use on this host.
from bleak.backends import get_default_backend
print(f"[ ok ] default backend: {get_default_backend()}")
PY

cat <<'EOF'

------------------------------------------------------------------
Ready. Next steps:

    source .venv/bin/activate          # Windows: .venv\Scripts\activate
    python main.py --help
    python main.py                    # scan, connect, print text, monitor 30s

Wear the glasses and make sure they are powered on and unpaired from the
Even Realities mobile app — the arms advertise as `G1_<channel>_<L|R>_<serial>`
and each arm is a separate BLE peripheral.
------------------------------------------------------------------
EOF