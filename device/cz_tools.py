#!/usr/bin/env python3
"""CardputerZero tool set: system info and control of the stock OS / APPLaunch.

One registry of plain functions, used three ways:
  - in-process by the voice layer        (import cz_tools; cz_tools.call(name, args))
  - from a shell                         (cz_tools.py list | call <name> '<json>' | diag)
  - over MCP by a remote LLM             (cz_mcp.py serves this registry)

APPLaunch has no external API on the shipped build, so UI control works the way
a person would: key events are injected into the real keyboard device and the
framebuffer is read back to see where we are. The centred carousel app is
identified by matching its icon against the launcher's own icon files.
"""
import base64
import glob
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
CONFIG = BASE / "config.json"
KBD = "/dev/input/by-path/platform-3f804000.i2c-event"
FB = "/dev/fb0"
W, H = 320, 170
APPLAUNCH = Path("/usr/share/APPLaunch")
BACKLIGHT = Path("/sys/class/backlight/backlight")
BATTERY = Path("/sys/class/power_supply/bq27220-0")

KEYS = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "enter": 28, "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "'": 40, "ctrl": 29, "shift": 42, "alt": 56, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, ".": 52, "/": 53, "space": 57, " ": 57, "up": 103, "left": 105, "right": 106, "down": 108,
}
# Launcher built-in pages: icon file stem -> app name shown in the carousel.
BUILTIN_ICONS = {
    "python": "Python", "store": "Store", "appstore": "Store", "cli": "CLI", "game": "Game",
    "setting": "Settings", "math": "Math", "calculator": "Math", "lora": "LoRa",
    "ip_panel": "IP Panel", "ssh": "SSH", "tank": "Tank", "file": "Files", "rec": "Rec",
    "camera": "Camera", "claw": "ZClaw", "music": "Music", "factory_test": "FactoryTest",
}

_ui_lock = threading.RLock()
TOOLS = {}


def tool(description, properties=None, required=()):
    def deco(fn):
        TOOLS[fn.__name__] = {
            "fn": fn, "description": description,
            "schema": {"type": "object", "properties": properties or {}, "required": list(required)},
        }
        return fn
    return deco


def call(name, args=None):
    if name not in TOOLS:
        raise KeyError(f"unknown tool: {name}")
    return TOOLS[name]["fn"](**(args or {}))


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default


def _state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(**kw):
    s = _state()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, indent=1))


# ---------------------------------------------------------------- low level UI

def _key(code, hold=0.05):
    fd = os.open(KBD, os.O_WRONLY)
    try:
        syn = struct.pack("llHHi", 0, 0, 0, 0, 0)
        os.write(fd, struct.pack("llHHi", 0, 0, 1, code, 1) + syn)
        time.sleep(hold)
        os.write(fd, struct.pack("llHHi", 0, 0, 1, code, 0) + syn)
    finally:
        os.close(fd)


def _screen():
    from PIL import Image
    with open(FB, "rb") as f:
        return Image.frombytes("RGB", (W, H), f.read(W * H * 2), "raw", "BGR;16")


def _diff(a, b):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _launcher_pid():
    out = _run(["pgrep", "-f", "M5CardputerZero-APPLaunch"])
    return int(out.split()[0]) if out else None


def _foreground():
    """Name of the external app the launcher is currently running, or None."""
    pid = _launcher_pid()
    if not pid:
        return None
    kids = {}
    for line in _run(["ps", "-e", "-o", "pid=,ppid=,args="]).splitlines():
        p = line.split(None, 2)
        if len(p) == 3:
            kids.setdefault(int(p[1]), []).append((int(p[0]), p[2]))
    todo, found = [pid], None
    while todo:
        for cpid, args in kids.get(todo.pop(), []):
            todo.append(cpid)
            words = [w for w in args.split() if not w.startswith("-")]
            cmd = next((os.path.basename(w) for w in words
                        if os.path.basename(w) not in ("sh", "bash", "dash", "sudo")), None)
            if cmd and cmd.startswith("."):
                cmd = "CLI shell"        # the built-in terminal runs: bash --rcfile ~/.bashrc
            if cmd and not cmd.startswith(("pw-", "aplay", "paplay")):
                found = cmd
    return found


_icons = None


def _icon_table():
    """{icon basename: (app name, 100x100 RGB image on black)} for every launcher icon."""
    global _icons
    if _icons is not None:
        return _icons
    from PIL import Image
    names = {}
    for f in glob.glob(str(APPLAUNCH / "applications" / "*.desktop")):
        txt = _read(f)
        n = re.search(r"^Name=(.+)$", txt, re.M)
        i = re.search(r"^Icon=(.+)$", txt, re.M)
        if n and i:
            names[os.path.basename(i.group(1).strip())] = n.group(1).strip()
    _icons = {}
    for f in glob.glob(str(APPLAUNCH / "share" / "images" / "*.png")):
        base = os.path.basename(f)
        stem = re.sub(r"(_100|_80|1)?\.png$", "", base)
        name = names.get(base) or BUILTIN_ICONS.get(stem)
        if not name:
            continue
        try:
            im = Image.open(f).convert("RGBA")
            if im.size[0] < 60 or abs(im.size[0] - im.size[1]) > 4:
                continue
            bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
            bg.alpha_composite(im)
            _icons[base] = (name, bg.convert("RGB").resize((100, 100)))
        except Exception:
            pass
    return _icons


_refs = None
CENTER_BOX = (122, 41, 198, 117)      # inside the centre carousel card
ESC_HOLD = 3.0                        # the CLI page needs longer than other apps; retries hold longer


def _icon_refs():
    """{app name: [19x19 thumbnails]} - low resolution makes the match tolerant of 1-2 px offsets."""
    global _refs
    if _refs is None:
        from PIL import Image
        _refs = {}
        for base, (name, icon) in _icon_table().items():
            r = icon.resize((92, 92), Image.BILINEAR).crop((8, 8, 84, 84)).resize((19, 19), Image.BILINEAR)
            _refs.setdefault(name, []).append(r)
    return _refs


def _match_center(img):
    """(score, app name) of the best icon match for the carousel centre. Real matches score under ~11,
    the best wrong app scores above ~33 (measured across all 24 stock apps)."""
    from PIL import Image
    c = img.crop(CENTER_BOX).resize((19, 19), Image.BILINEAR)
    return min((min(_diff(c, r) for r in rs), n) for n, rs in _icon_refs().items())


def _is_home(img):
    """The home screen draws a grey frame around the centre card at fixed pixels."""
    px = img.load()
    g = lambda x, y: sum(px[x, y]) // 3
    frame = [g(111, 80), g(208, 80), g(160, 30), g(160, 127)]
    return all(45 <= v <= 90 for v in frame) and g(104, 80) < 20 and g(160, 24) < 20


def _lock_metrics(img):
    from PIL import ImageStat
    bar = img.crop((0, 0, 150, 20)).resize((30, 4))
    amber = sum(1 for r, g_, b in bar.getdata() if r > 150 and g_ > 110 and b < 90)
    body = img.crop((0, 24, W, H)).resize((32, 15))
    return amber, round(sum(ImageStat.Stat(body).stddev) / 3, 1)


def _lock_like(img):
    """Lock panel: amber 'Press ... to unlock' text top-left over a photo wallpaper."""
    amber, spread = _lock_metrics(img)
    return amber >= 3 and spread > 25


def _ui_state(img=None):
    img = img or _screen()
    from PIL import ImageStat
    if sum(ImageStat.Stat(img).mean) / 3 < 2.0:
        return {"screen": "asleep"}
    if _is_home(img):            # checked first: the CLI page leaves a shell running behind the home screen
        score, name = _match_center(img)
        return {"screen": "home", "selected": name if score < 20 else None, "match": round(score, 1)}
    fg = _foreground()
    if fg:
        return {"screen": "app", "app": fg}
    return {"screen": "locked" if _lock_like(img) else "page"}


def _wake_sequence():
    _key(KEYS["ctrl"])           # any key shows the lock panel (ctrl has no side effect)
    time.sleep(0.6)
    _key(KEYS["tab"])            # TAB arms it
    time.sleep(0.3)
    _key(KEYS["enter"])          # ENTER unlocks
    time.sleep(0.9)


def _unlock():
    st = _ui_state()
    if st["screen"] == "locked":
        _key(KEYS["esc"])        # back to a known state: screen off
        time.sleep(0.6)
        st = _ui_state()
    if st["screen"] == "asleep":
        _wake_sequence()
        st = _ui_state()
    return st


def _go_home():
    st = _ui_state()
    for attempt in range(4):
        if st["screen"] == "home":
            return st
        if st["screen"] == "asleep":
            _wake_sequence()
        else:                    # app, page or lock panel: ESC long-press
            _key(KEYS["esc"], hold=ESC_HOLD + attempt * 1.5)
            time.sleep(1.3)
        st = _ui_state()
    return st


def _walk():
    """Step right through the whole carousel; returns app names in RIGHT order."""
    order = []
    for _ in range(60):
        time.sleep(0.55)
        st = _ui_state()
        if st["screen"] != "home":
            break
        if order and st["selected"] == order[0] and len(order) > 2:
            break
        order.append(st["selected"])
        _key(KEYS["right"])
    _save_state(order=order)
    return order


# ---------------------------------------------------------------- tools: system

@tool("System status: temperature, CPU load, memory, swap, disk, uptime, OS and network address.")
def system_info():
    mem = {k: int(v.split()[0]) // 1024 for k, v in
           (l.split(":") for l in _read("/proc/meminfo").splitlines()) if k in
           ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}
    du = shutil.disk_usage("/")
    temp = _read("/sys/class/thermal/thermal_zone0/temp", "0")
    return {
        "hostname": os.uname().nodename,
        "temperature_c": round(int(temp) / 1000, 1),
        "throttled": _run(["vcgencmd", "get_throttled"]).replace("throttled=", ""),
        "load_1m_5m_15m": _read("/proc/loadavg").split()[:3],
        "uptime_minutes": int(float(_read("/proc/uptime", "0 0").split()[0]) // 60),
        "memory_mb": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
                      "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)},
        "disk_gb": {"total": round(du.total / 1e9, 1), "free": round(du.free / 1e9, 1)},
        "kernel": os.uname().release,
        "os": (re.search(r'PRETTY_NAME="(.+)"', _read("/etc/os-release")) or [None, "?"])[1],
        "ip": _run(["hostname", "-I"]).split()[:1],
    }


@tool("Battery charge percent, charging state and voltage.")
def battery():
    return {
        "percent": int(_read(BATTERY / "capacity", "-1")),
        "status": _read(BATTERY / "status", "unknown"),
        "voltage_v": round(int(_read(BATTERY / "voltage_now", "0")) / 1e6, 2),
        "current_ma": round(int(_read(BATTERY / "current_now", "0")) / 1000),
    }


@tool("Wi-Fi connection: network name, signal strength, band, link rate, IP address.")
def wifi_status():
    for line in _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,FREQ,RATE", "dev", "wifi"]).splitlines():
        p = line.split(":")
        if p[0] == "yes":
            return {"connected": True, "ssid": p[1], "signal_percent": int(p[2]), "freq": p[3],
                    "rate": p[4], "ip": _run(["hostname", "-I"]).split()[:1]}
    return {"connected": False}


@tool("Read the speaker volume (0-100) and mute state.")
def get_volume():
    out = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    m = re.search(r"([\d.]+)", out)
    return {"percent": round(float(m.group(1)) * 100) if m else None, "muted": "MUTED" in out}


@tool("Set the speaker volume.", {"percent": {"type": "integer", "minimum": 0, "maximum": 100}}, ["percent"])
def set_volume(percent):
    percent = max(0, min(100, int(percent)))
    _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
    _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}"])
    return get_volume()


@tool("Turn the Wi-Fi radio on or off. Off also drops Tailscale and SSH until it is turned back on.",
      {"on": {"type": "boolean"}}, ["on"])
def wifi_power(on):
    _run(["sudo", "-n", "nmcli", "radio", "wifi", "on" if on else "off"])
    state = _run(["nmcli", "-t", "-f", "WIFI", "radio"]).strip()
    return {"ok": state == ("enabled" if on else "disabled"), "wifi": state}


@tool("Read the screen backlight brightness (0-100).")
def get_brightness():
    mx = int(_read(BACKLIGHT / "max_brightness", "100"))
    return {"percent": round(int(_read(BACKLIGHT / "brightness", "0")) * 100 / mx)}


@tool("Set the screen backlight brightness.", {"percent": {"type": "integer", "minimum": 0, "maximum": 100}}, ["percent"])
def set_brightness(percent):
    mx = int(_read(BACKLIGHT / "max_brightness", "100"))
    val = str(round(max(0, min(100, int(percent))) * mx / 100))
    try:
        (BACKLIGHT / "brightness").write_text(val)
    except PermissionError:
        subprocess.run(["sudo", "-n", "tee", str(BACKLIGHT / "brightness")], input=val, text=True,
                       capture_output=True, timeout=5)
    return get_brightness()


# ---------------------------------------------------------------- direct launcher control
# The rebuilt launcher (automation RPC patch) answers on a local ZeroMQ socket: apps / current / launch / home.
# When it is there, apps open in well under a second. On a stock launcher these helpers report "not available"
# and the tools fall back to pressing keys and reading the screen.

RPC_ENDPOINT = "tcp://127.0.0.1:5557"
NAME_ALIASES = {"math": "Calculator", "game": "Snake", "rec": "Rec", "terminal": "CLI"}
_rpc_seen = {"ok": False, "at": 0.0}


def _rpc(text, timeout_ms=4000):
    try:
        import zmq
    except ImportError:
        return None
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, 1000)
    try:
        sock.connect(RPC_ENDPOINT)
        sock.send_string(text)
        body = sock.recv_multipart()[0].decode(errors="replace")
        return body.splitlines() if body.startswith("OK") else None
    except Exception:
        return None
    finally:
        sock.close()


def _rpc_available():
    if time.time() - _rpc_seen["at"] > 30:
        _rpc_seen.update(ok=_rpc("ping", 600) is not None, at=time.time())
    return _rpc_seen["ok"]


def _rpc_current():
    return dict(l.split("=", 1) for l in (_rpc("current") or [])[1:] if "=" in l)


def _open_app_rpc(name):
    apps = (_rpc("apps") or [""])[1:]
    if not apps:
        return None
    key = NAME_ALIASES.get(name.lower(), name).lower()
    want = next((a for a in apps if a.lower() == key), None) or next((a for a in apps if key in a.lower()), None)
    if not want:
        return {"ok": False, "error": f"no app named {name!r}", "apps": apps}
    _unlock()                                   # waking and unlocking the screen still goes through the keyboard
    if _rpc_current().get("mode") not in ("home", None):
        _rpc("home", 8000)
        time.sleep(0.8)
    if not _rpc(f"launch {want}", 8000):
        return None
    time.sleep(0.7)
    cur = _rpc_current()
    return {"ok": True, "opened": want, "via": "launcher", "state": {"screen": cur.get("mode"), "app": cur.get("foreground")}}


# ---------------------------------------------------------------- tools: UI / apps

@tool("What is on screen now: asleep, locked, home (with the selected app), an internal page, or a running app.")
def screen_state():
    with _ui_lock:
        return _ui_state()


@tool("List the apps in the launcher carousel. Pass refresh=true after installing or removing apps.",
      {"refresh": {"type": "boolean"}})
def list_apps(refresh=False):
    with _ui_lock:
        if _rpc_available():
            apps = (_rpc("apps") or [""])[1:]
            if apps:
                return {"apps": apps, "via": "launcher"}
        order = _state().get("order")
        if refresh or not order:
            st = _go_home()
            if st["screen"] != "home":
                return {"error": "could not reach the home screen", "state": st}
            order = _walk()
        return {"apps": order}


@tool("Open an app from the launcher by name, e.g. Settings, Camera, Store, CLI, Music, Files.",
      {"name": {"type": "string"}}, ["name"])
def open_app(name):
    with _ui_lock:
        if _rpc_available():
            res = _open_app_rpc(name)
            if res is not None:
                return res
        st = _go_home()
        if st["screen"] != "home":
            return {"ok": False, "error": "could not reach the home screen", "state": st}
        order = _state().get("order") or _walk()
        want = next((a for a in order if a.lower() == name.lower()), None) or \
            next((a for a in order if name.lower() in a.lower()), None)
        if not want:
            return {"ok": False, "error": f"no app named {name!r}", "apps": order}
        cur = _ui_state().get("selected")
        if cur in order and cur != want:
            d = (order.index(want) - order.index(cur)) % len(order)
            code, n = (KEYS["right"], d) if d <= len(order) // 2 else (KEYS["left"], len(order) - d)
            for _ in range(n):
                _key(code)
                time.sleep(0.3)
            time.sleep(0.5)
        for _ in range(len(order) + 2):         # verify, and step the slow way if we are off
            if _ui_state().get("selected") == want:
                break
            _key(KEYS["right"])
            time.sleep(0.55)
        else:
            return {"ok": False, "error": f"could not select {want}"}
        _key(KEYS["enter"])
        time.sleep(1.8)
        return {"ok": True, "opened": want, "state": _ui_state()}


@tool("Close whatever app or page is open and return to the launcher home screen.")
def go_home():
    with _ui_lock:
        if _rpc_available():
            _unlock()
            if _rpc("home", 8000):
                time.sleep(0.6)
                cur = _rpc_current()
                if cur.get("mode") == "home":
                    return {"ok": True, "via": "launcher", "state": {"screen": "home", "selected": cur.get("selected")}}
        st = _go_home()
        return {"ok": st["screen"] == "home", "state": st}


@tool("Wake and unlock the screen.")
def unlock_screen():
    with _ui_lock:
        return _unlock()


@tool("Turn the screen off and lock it, the same as holding TAB for three seconds on the launcher. "
      "Only works from the home screen or a launcher page, not while an app is open.")
def sleep_screen():
    with _ui_lock:
        st = _ui_state()
        if st["screen"] == "asleep":
            return {"ok": True, "already": True, "state": st}
        if st["screen"] == "app":
            return {"ok": False, "reason": "an app is open", "state": st}
        if st["screen"] == "locked":             # lock panel showing: ESC sends it back to black
            _key(KEYS["esc"])
        elif _rpc_available() and _rpc("key %d 1 0" % KEYS["tab"]):
            try:                                 # held inside the launcher, so the voice layer's TAB trigger never sees it
                time.sleep(3.4)
            finally:
                _rpc("key %d 0 0" % KEYS["tab"])
        else:
            return {"ok": False, "reason": "launcher control is not available", "state": st}
        time.sleep(0.7)
        st = _ui_state()
        return {"ok": st["screen"] == "asleep", "state": st}


@tool("Press keys in order, as if typed on the keyboard. Names: enter, esc, tab, up, down, left, right, "
      "space, backspace, shift, letters, digits. Use this to move through menus such as Settings.",
      {"keys": {"type": "array", "items": {"type": "string"}},
       "hold_seconds": {"type": "number", "description": "hold each key this long (default 0.05)"}}, ["keys"])
def press_keys(keys, hold_seconds=0.05):
    with _ui_lock:
        bad = [k for k in keys if k.lower() not in KEYS]
        if bad:
            return {"ok": False, "error": f"unknown keys: {bad}", "known": sorted(KEYS)}
        for k in keys:
            _key(KEYS[k.lower()], hold=min(float(hold_seconds), 5.0))
            time.sleep(0.25)
        return {"ok": True, "state": _ui_state()}


SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/"}


def _key_shifted(code):
    fd = os.open(KBD, os.O_WRONLY)
    try:
        ev = lambda c, v: struct.pack("llHHi", 0, 0, 1, c, v) + struct.pack("llHHi", 0, 0, 0, 0, 0)
        os.write(fd, ev(KEYS["shift"], 1)); time.sleep(0.04)
        os.write(fd, ev(code, 1)); time.sleep(0.04)
        os.write(fd, ev(code, 0)); time.sleep(0.03)
        os.write(fd, ev(KEYS["shift"], 0))
    finally:
        os.close(fd)


@tool("Type text into the focused field: letters (either case), digits, space and common punctuation.",
      {"text": {"type": "string"}}, ["text"])
def type_text(text):
    with _ui_lock:
        skipped = []
        for ch in text:
            if ch in SHIFTED:
                _key_shifted(KEYS[SHIFTED[ch]])
            elif ch.isalpha() and ch.isupper() and ch.lower() in KEYS:
                _key_shifted(KEYS[ch.lower()])
            elif len(ch) == 1 and ch in KEYS:
                _key(KEYS[ch])
            else:
                skipped.append(ch)
                continue
            time.sleep(0.06)
        return {"ok": not skipped, "skipped": skipped}


@tool("Capture the screen as a PNG image (320x170).")
def screenshot():
    buf = io.BytesIO()
    _screen().save(buf, "PNG")
    return {"_image_png_b64": base64.b64encode(buf.getvalue()).decode()}


# ---------------------------------------------------------------- tools: wake word / power

@tool("Wake word listener. action: status | enable | disable | set_model. One wake word is active at a time; "
      "set_model switches it. Models: hey_m5_v3 (Hey M5), hey_frank_v5 (Hey Frank), hey_stackchan_v1 (Hey StackChan).",
      {"action": {"type": "string", "enum": ["status", "enable", "disable", "set_model"]},
       "model": {"type": "string"}}, ["action"])
def wake_word(action, model=None):
    have = sorted(p.stem for p in (BASE / "models").glob("*.json"))
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if action == "enable":
        _run(["systemctl", "--user", "enable", "--now", "cz-voice.service"])
    elif action == "disable":
        _run(["systemctl", "--user", "disable", "--now", "cz-voice.service"])
    elif action == "set_model":
        pick = next((m for m in have if model and (m == model or model.lower().replace(" ", "_") in m)), None)
        if not pick:
            return {"ok": False, "error": f"unknown model: {model!r}", "available": have}
        cfg.setdefault("wake", {})["models"] = [pick]
        CONFIG.write_text(json.dumps(cfg, indent=1))
    last = _read(BASE / "logs" / "wake.jsonl").splitlines()[-3:]
    return {"active": _run(["systemctl", "--user", "is-active", "cz-voice.service"]) == "active",
            "model": (cfg.get("wake", {}).get("models") or ["okay_nabu"])[0], "available": have,
            "recent_detections": [json.loads(l) for l in last if l.strip()]}


@tool("Dictation engine: action = status | set. engine 'local' recognises on the device (no punctuation); "
      "'server' sends audio to the Whisper server on the network. Hold ALT to dictate.",
      {"action": {"type": "string", "enum": ["status", "set"]}, "engine": {"type": "string", "enum": ["local", "server"]}},
      ["action"])
def dictation(action, engine=None):
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if action == "set":
        if engine not in ("local", "server"):
            return {"ok": False, "error": "engine must be local or server"}
        cfg.setdefault("dictation", {})["engine"] = engine
        CONFIG.write_text(json.dumps(cfg, indent=1))
    return {"engine": cfg.get("dictation", {}).get("engine", "local")}


@tool("Reboot or shut down the device. Requires confirm=true.",
      {"action": {"type": "string", "enum": ["reboot", "shutdown"]}, "confirm": {"type": "boolean"}},
      ["action", "confirm"])
def power(action, confirm=False):
    if not confirm:
        return {"ok": False, "error": "confirm must be true"}
    subprocess.Popen(["sudo", "-n", "systemctl", "reboot" if action == "reboot" else "poweroff"])
    return {"ok": True, "action": action}


# ---------------------------------------------------------------- CLI

def _diag():
    img = _screen()
    from PIL import ImageStat
    print("mean brightness:", round(sum(ImageStat.Stat(img).mean) / 3, 1))
    print("foreground:", _foreground())
    print("icons known:", len(_icon_table()))
    print("home frame:", _is_home(img), "| centre match:", _match_center(img))
    print("lock metrics (amber, spread):", _lock_metrics(img), "| state:", _ui_state(img))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for n, t in TOOLS.items():
            print(f"{n:16} {t['description']}")
    elif cmd == "diag":
        _diag()
    elif cmd == "call":
        res = call(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
        if isinstance(res, dict) and "_image_png_b64" in res:
            out = "/tmp/cz_screenshot.png"
            Path(out).write_bytes(base64.b64decode(res["_image_png_b64"]))
            res = {"saved": out}
        print(json.dumps(res, indent=1))
    else:
        sys.exit("usage: cz_tools.py list | diag | call <tool> ['<json args>']")
