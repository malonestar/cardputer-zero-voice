#!/bin/bash
# Reverses install.sh. Run on the device. Leaves $CZ/tars_password, the "tars" section of config.json and the
# python-socketio package alone (pass --purge-pip to remove the package too).
# Test overrides: CZ_DIR, APPLAUNCH, SUDO (set empty to skip sudo), SYSTEMCTL, PIP.
set -uo pipefail
CZ="${CZ_DIR:-$HOME/cz-ai}"
AL="${APPLAUNCH:-/usr/share/APPLaunch}"
SUDO="${SUDO-sudo}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PIP="${PIP:-$CZ/venv/bin/pip}"
did=()

$SUDO rm -f "$AL/applications/tars.desktop" "$AL/share/images/tars_100.png" && did+=("removed launcher entry and icon")
rm -rf "$CZ/tars" && did+=("removed $CZ/tars")
rm -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/cz-voice.pause"

if [ -f "$CZ/voice.py.bak-tars" ]; then
  cp -p "$CZ/voice.py.bak-tars" "$CZ/voice.py" && did+=("restored voice.py from voice.py.bak-tars (backup file kept)")
else
  did+=("no voice.py.bak-tars found: voice.py left as it is")
fi
if $SYSTEMCTL --user restart cz-voice.service; then did+=("restarted cz-voice.service"); else did+=("WARNING: could not restart cz-voice.service"); fi
if [ "${1:-}" = "--purge-pip" ]; then "$PIP" uninstall -y python-socketio python-engineio >/dev/null 2>&1 && did+=("removed python-socketio from the venv"); fi

echo "TARS remote uninstall:"
for d in "${did[@]}"; do echo "  - $d"; done
