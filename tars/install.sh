#!/bin/bash
# Installs TARS remote on the CardputerZero. Run ON THE DEVICE, as the normal user, from the folder holding these files.
# Safe to run twice. Test overrides: CZ_DIR, APPLAUNCH, SUDO (set empty to skip sudo), SYSTEMCTL, PIP.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CZ="${CZ_DIR:-$HOME/cz-ai}"
APP="$CZ/tars"
AL="${APPLAUNCH:-/usr/share/APPLaunch}"
SUDO="${SUDO-sudo}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PIP="${PIP:-$CZ/venv/bin/pip}"
done_=()

for f in tars_remote.py tars-remote tars.desktop tars_100.png voice.py; do
  [ -f "$SRC/$f" ] || { echo "missing $SRC/$f - run this from the folder with the TARS remote files" >&2; exit 1; }
done
[ -x "$PIP" ] || { echo "no venv at $CZ/venv (expected $PIP)" >&2; exit 1; }
[ -f "$CZ/voice.py" ] || { echo "no $CZ/voice.py - is this the CardputerZero?" >&2; exit 1; }

# 1. python-socketio (brings requests and websocket-client). A second run finds it installed and does nothing.
"$PIP" install --quiet "python-socketio[client]"
done_+=("python-socketio[client] present in $CZ/venv")

# 2. app files
mkdir -p "$APP"
install -m 644 "$SRC/tars_remote.py" "$APP/tars_remote.py"
install -m 755 "$SRC/tars-remote" "$APP/tars-remote"
[ -f "$SRC/tars_bg.png" ] && install -m 644 "$SRC/tars_bg.png" "$APP/tars_bg.png"   # optional background picture
done_+=("app installed to $APP (tars_remote.py, tars-remote)")

# 3. launcher entry: icon + .desktop (Exec points at the installed wrapper)
$SUDO mkdir -p "$AL/share/images" "$AL/applications"
$SUDO install -m 644 "$SRC/tars_100.png" "$AL/share/images/tars_100.png"
sed "s#^Exec=.*#Exec=$APP/tars-remote#" "$SRC/tars.desktop" > "${TMPDIR:-/tmp}/tars.desktop.$$"
$SUDO install -m 644 "${TMPDIR:-/tmp}/tars.desktop.$$" "$AL/applications/tars.desktop"
rm -f "${TMPDIR:-/tmp}/tars.desktop.$$"
done_+=("launcher entry: $AL/applications/tars.desktop and share/images/tars_100.png")

# 4. voice.py with the pause hand-over. The first backup is never overwritten.
if cmp -s "$SRC/voice.py" "$CZ/voice.py"; then
  done_+=("voice.py already has the pause change (unchanged)")
else
  if [ -e "$CZ/voice.py.bak-tars" ]; then
    cp -p "$CZ/voice.py" "$CZ/voice.py.bak-tars.$(date +%Y%m%d%H%M%S)"
    done_+=("voice.py differed from the new one; extra backup made next to the existing voice.py.bak-tars")
  else
    cp -p "$CZ/voice.py" "$CZ/voice.py.bak-tars"
    done_+=("backed up voice.py to $CZ/voice.py.bak-tars")
  fi
  python3 -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$SRC/voice.py"
  install -m 644 "$SRC/voice.py" "$CZ/voice.py"
  done_+=("voice.py replaced with the pause-aware version")
fi

# 5. restart the voice layer so it picks up the change
if $SYSTEMCTL --user restart cz-voice.service; then
  done_+=("restarted cz-voice.service")
else
  done_+=("WARNING: could not restart cz-voice.service - restart it yourself: systemctl --user restart cz-voice.service")
fi

echo "TARS remote install:"
for d in "${done_[@]}"; do echo "  - $d"; done
echo
echo "Still to do by hand:"
echo "  - put the robot web UI password (first line) in $CZ/tars_password   (chmod 600)"
echo "  - optional settings go in $CZ/config.json under \"tars\" (see NOTES.md)"
echo "  - the launcher may need a restart or a rescan to show the new TARS icon"
