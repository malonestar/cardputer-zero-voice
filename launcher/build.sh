#!/bin/bash
# Builds the CardputerZero launcher (tag launcher-v0.6.105) with the automation RPC patch, cross-compiled for aarch64.
# Mirrors .github/workflows/launcher-build.yml from the launcher repo. Run inside WSL Ubuntu.
set -uo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"; W=$HOME/cz-launcher-build; mkdir -p "$W"; cd "$W"
step() { echo "[$(date +%T)] $*"; }
step "toolchain: $(aarch64-linux-gnu-g++ --version 2>/dev/null | head -1 || echo MISSING)"
[ -d venv ] || python3 -m venv venv
venv/bin/pip -q install scons parse requests tqdm 2>&1 | tail -1
export PATH="$W/venv/bin:$PATH"
if [ ! -d launcher ]; then step "clone"; git clone -q --branch launcher-v0.6.105 --depth 1 https://github.com/CardputerZero/launcher || exit 11; fi
cd launcher
step "submodules"; git submodule update --init --depth=1 SDK 2>&1 | tail -1
if ! git apply --reverse --check "$SRC/rpc_commands.patch" 2>/dev/null; then step "apply patch"; git apply "$SRC/rpc_commands.patch" || exit 12; fi
DEST=SDK/github_source/C-Thread-Pool
if [ ! -d "$DEST" ]; then step "thread pool"; curl -fsSL -o /tmp/ctp.zip https://github.com/Pithikos/C-Thread-Pool/archive/refs/heads/master.zip && rm -rf /tmp/ctp && unzip -q /tmp/ctp.zip -d /tmp/ctp && mkdir -p SDK/github_source && mv /tmp/ctp/C-Thread-Pool-master "$DEST"; fi
cd projects/APPLaunch
export CardputerZero=y CONFIG_REPO_AUTOMATION=y APPLAUNCH_VERSION=0.6.105 APPLAUNCH_CHANNEL=local
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
step "scons build start"
scons -j16 > "$W/scons.log" 2>&1; RC=$?
step "scons exit code $RC"
tail -25 "$W/scons.log"
BIN=$(find dist build -type f -name 'M5CardputerZero-APPLaunch' 2>/dev/null | head -1)
if [ -n "$BIN" ]; then file "$BIN"; ls -la "$BIN"; cp "$BIN" "$SRC/M5CardputerZero-APPLaunch.rpc"; step "copied to $SRC"; fi
grep -n -E 'ZMQ_RPC|INIT_RPC' build/config/global_config.mk 2>/dev/null | head -4
step DONE
