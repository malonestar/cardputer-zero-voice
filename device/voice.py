#!/usr/bin/env python3
"""Voice layer for the CardputerZero, running above whatever app is on screen.

Triggers:  wake word, or TAB held        -> command mode
           ALT held (push-to-talk)       -> dictation: speech is typed into the focused field
Command:   on-device streaming recognition (sherpa-onnx) -> fuzzy command match -> local tool.
           No match -> the utterance is saved and handed to the `on_unmatched` hook (xiaozhi).
Dictation: on-device by default ("local"); "server" posts the audio to a Whisper endpoint.
Keys are read passively from the keyboard device; nothing is grabbed.
"""
import difflib, glob, hashlib, io, json, math, os, re, struct, subprocess, sys, threading, time, urllib.request, uuid, wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
CONFIG, LOGS, SND = BASE / "config.json", BASE / "logs", BASE / "sounds"
KBD = "/dev/input/by-path/platform-3f804000.i2c-event"
RATE, CHUNK = 16000, 640
KEY_TAB, KEY_ALT = 15, 56
DEFAULTS = {
    "wake": {"models": ["okay_nabu"], "cutoff": None, "enabled": True},
    "voice": {"tab_hold_s": 0.6, "silence_s": 0.9, "max_utterance_s": 8, "match_cutoff": 0.8,
              "speak": True, "speak_actions": False, "overlay": True, "on_unmatched": None, "engine": "server", "kws": True,
              "tts_url": "http://YOUR-SPEECH-SERVER:8001/v1/audio/speech",
              "tts_model": "speaches-ai/piper-en_US-ryan-high", "tts_voice": "ryan"},
    "dictation": {"engine": "server", "hold_s": 0.35, "max_s": 60,
                  "url": "http://YOUR-SPEECH-SERVER:8001/v1/audio/transcriptions",
                  "model": "Systran/faster-whisper-small"},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        for k, v in json.loads(CONFIG.read_text()).items():
            cfg.setdefault(k, {}).update(v)
    except Exception:
        pass
    return cfg


def log(event, **kw):
    LOGS.mkdir(exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    print(json.dumps(rec), flush=True)
    with (LOGS / "voice.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------ sound

TONES = {"wake": [(880, .07), (1320, .10)], "ok": [(1320, .06), (1760, .09)], "nomatch": [(660, .09), (440, .12)],
         "dictate": [(1000, .05)], "error": [(300, .25)], "empty": [(500, .08)],
         "wifi_on": [(660, .08), (990, .08), (1320, .14)], "wifi_off": [(1320, .08), (990, .08), (660, .14)]}


def make_sounds():
    SND.mkdir(exist_ok=True)
    for name, notes in TONES.items():
        path = SND / f"{name}.wav"
        if path.exists():
            continue
        frames = bytearray()
        for freq, dur in notes:
            n = int(RATE * dur)
            for i in range(n):
                env = min(1.0, i / 200, (n - i) / 400)
                frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / RATE)))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(bytes(frames))


def play(name):
    subprocess.Popen(["pw-play", str(SND / f"{name}.wav")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def say(text, cfg):
    """Spoken confirmation in a natural voice: Piper on the home server, cached on the device per phrase.
    If the server cannot be reached the confirmation tone that already played is the only feedback."""
    v = cfg["voice"]
    if not v["speak"] or not text:
        return
    cache = SND / "tts"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / (hashlib.sha1(f"{v['tts_model']}|{v['tts_voice']}|{text}".encode()).hexdigest()[:16] + ".wav")
    try:
        if not f.exists():
            body = json.dumps({"model": v["tts_model"], "voice": v["tts_voice"], "input": text,
                               "response_format": "wav"}).encode()
            req = urllib.request.Request(v["tts_url"], body, {"Content-Type": "application/json"})
            f.write_bytes(urllib.request.urlopen(req, timeout=8).read())
        subprocess.Popen(["pw-play", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


ANSWERS = {"battery", "temperature", "wifi", "status", "time", "wake"}   # the reply is the point, so it is spoken


def confirm(intent, spoken, cfg):
    """Actions (open an app, go home, volume, brightness) get the confirmation tone only.
    Speech is kept for questions and for failures. voice.speak_actions = true brings the old behaviour back."""
    if intent in ANSWERS or spoken.startswith("Could not") or cfg["voice"].get("speak_actions"):
        say(spoken, cfg)
        return True
    return False


# ------------------------------------------------------------------ on-screen indicator (overlay.py)

OV = None                                        # created in main(); stays None for the command-line test modes
SCREEN_CHANGING = {"open", "home", "unlock", "sleep"}    # the pill is cleared before these, so it never puts back stale pixels


def ov(kind, text, hold=None):
    if OV:
        OV.show(kind, " ".join(str(text).split()), hold)


def ov_hide():
    if OV:
        OV.hide()


def show_match(intent, args, heard, cfg):
    """Green confirmation of a matched command; `heard` is the transcript when there is one."""
    label = heard or ("Open " + args["name"] if intent == "open" else
                      intent.replace("_", " ").capitalize().replace("Wifi", "Wi-Fi"))
    if intent in SCREEN_CHANGING:
        ov("ok", label)
        time.sleep(cfg["voice"].get("overlay_flash_s", 0.35) if OV else 0)
        ov_hide()
    else:
        ov("ok", label, cfg["voice"].get("overlay_hold_s", 1.4))


def show_result(intent, spoken):
    if spoken.startswith("Could not"):
        ov("error", spoken, 2.5)
    elif intent in ANSWERS and spoken:           # the answer is readable even when the spoken voice is unavailable
        ov("ok", spoken, 3.0)


# ------------------------------------------------------------------ tools (via the local MCP server)

def tool(name, args=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args or {}}}).encode()
    req = urllib.request.Request("http://127.0.0.1:8765/mcp", body, {
        "Content-Type": "application/json", "Authorization": "Bearer " + (BASE / "token").read_text().strip()})
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.load(r)
    text = out.get("result", {}).get("content", [{}])[0].get("text", "{}")
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


# ------------------------------------------------------------------ command matching

ALIASES = {
    "Settings": ["settings", "setting", "preferences", "options"], "CLI": ["cli", "terminal", "command line", "shell", "console"],
    "Rec": ["rec", "recorder", "voice recorder", "recording"], "Math": ["math", "calculator", "calc"],
    "Files": ["files", "file manager", "file browser"], "Store": ["store", "app store", "shop"],
    "ZClaw": ["z claw", "zclaw", "claw"], "IP Panel": ["ip panel", "i p panel", "network panel"],
    "SSH": ["ssh", "s s h", "secure shell"], "Game": ["game", "games", "snake"], "Tank": ["tank", "tanks", "tank battle"],
    "Keyboard Guide": ["keyboard guide", "key guide", "keyboard help"], "FactoryTest": ["factory test"],
    "GPS": ["gps", "g p s"], "LoRa": ["lora", "lo ra", "laura", "lora chat"], "NFC": ["nfc", "n f c"],
    "SubG Chat": ["sub g chat", "subg chat", "sub gig chat", "sub g"],
    "IR Remote": ["ir remote", "i r remote", "infrared remote", "remote", "remote control", "tv remote"],
    "IR Chat": ["ir chat", "i r chat", "infrared chat"], "Camera": ["camera", "cam"],
    "Music": ["music", "music player", "songs"], "Piano": ["piano"], "Compass": ["compass"], "Python": ["python"],
}
OPEN_VERBS = ["switch to", "bring up", "pull up", "talk to", "go to", "open up", "open", "launch", "start", "run", "show"]
INTENTS = {
    "home": ["go home", "home", "close", "close this", "close it", "exit", "go back", "quit", "back to home"],
    "volume_up": ["volume up", "louder", "turn it up", "turn up volume", "increase volume", "raise volume"],
    "volume_down": ["volume down", "quieter", "turn it down", "turn down volume", "lower volume", "decrease volume"],
    "mute": ["mute", "silence", "be quiet"],
    "brightness_up": ["brightness up", "brighter", "increase brightness", "turn up brightness", "raise brightness"],
    "brightness_down": ["brightness down", "dimmer", "dim", "decrease brightness", "turn down brightness", "lower brightness"],
    "battery": ["battery", "battery level", "how much battery", "how much battery is left", "whats battery",
                "battery status", "battery percentage"],
    "temperature": ["cpu temperature", "device temperature", "system temperature", "how hot are you", "whats your temperature"],
    "wifi": ["wifi", "wifi status", "am i connected", "whats ip", "ip address", "network status", "whats ip address"],
    "status": ["system status", "status", "system info", "how much memory", "memory", "status report"],
    "time": ["what time is it", "time", "whats time", "current time"],
    "wifi_on": ["turn on wifi", "turn wifi on", "wifi on", "enable wifi", "turn on wireless", "wireless on",
                "turn on network", "network on", "go online"],
    "wifi_off": ["turn off wifi", "turn wifi off", "wifi off", "disable wifi", "turn off wireless", "wireless off",
                 "turn off network", "network off", "go offline"],
    "unlock": ["unlock", "unlock it", "unlock device", "wake up", "wake", "turn on display"],
    "sleep": ["sleep", "go to sleep", "sleep now", "sleep mode", "lock", "lock it", "lock device", "turn off display"],
}
FILLER = {"the", "a", "an", "please", "app", "application", "hey", "can", "you", "could", "would", "me", "my",
          "now", "just", "for", "of", "screen"}
NUMS = {"zero": 0, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "thirty": 30, "forty": 40,
        "fifty": 50, "sixty": 60, "seventy": 70, "seventy five": 75, "eighty": 80, "ninety": 90, "hundred": 100,
        "one hundred": 100, "half": 50, "max": 100, "maximum": 100, "full": 100, "minimum": 0}


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", text.lower().replace("'", ""))).strip()


def strip_filler(t):
    return " ".join(w for w in t.split() if w not in FILLER)


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


_apps_cache = {"at": 0.0, "names": []}


def installed_apps():
    """App names straight from the launcher, so newly installed apps can be opened by voice without edits."""
    if time.time() - _apps_cache["at"] > 30:
        try:
            names = tool("list_apps").get("apps") or []
        except Exception:
            names = []
        _apps_cache.update(at=time.time(), names=names)
    return _apps_cache["names"]


def best_app(t):
    best = (None, 0.0)
    for app, names in ALIASES.items():
        for n in names:
            r = ratio(t, n)
            if r > best[1]:
                best = (app, r)
    squashed = t.replace(" ", "")
    for app in installed_apps():                 # "raspy jack" should still find "RaspyJack"
        n = norm(app)
        r = max(ratio(t, n), ratio(squashed, n.replace(" ", "")))
        if r > best[1]:
            best = (app, r)
    return best


def match(text, cutoff=0.8):
    """Returns (intent, args) or None. Deterministic: verb + app name, number commands, then phrase lists."""
    t = norm(text)
    if not t:
        return None
    for v in OPEN_VERBS:
        if t.startswith(v + " "):
            app, r = best_app(strip_filler(t[len(v) + 1:]))
            if r >= 0.72:
                return ("open", {"name": app})
            break                                # "go to sleep" starts like "go to <app>": try the other commands
    if "wake word" in t or "wakeword" in t:
        for key, model in (("frank", "hey_frank_v5"), ("stack", "hey_stackchan_v1"), ("m five", "hey_m5_v3"), ("m5", "hey_m5_v3")):
            if key in t:
                return ("wake", {"model": model})
        return None
    m = re.search(r"\b(volume|brightness)\b(?: to| at)? (.+)$", t)
    if m:
        val = m.group(2).replace(" percent", "").strip()
        num = int(val) if val.isdigit() else NUMS.get(val)
        if num is not None:
            return ("set_" + m.group(1), {"percent": max(0, min(100, num))})
    s = strip_filler(t)
    best = (None, 0.0)
    for intent, phrases in INTENTS.items():
        for p in phrases:
            r = ratio(s, p)
            if r > best[1]:
                best = (intent, r)
    if best[0] == "unlock" and not any(w in s for w in ("unlock", "wake", "display")):
        best = (None, 0.0)                       # "lock" is one letter away from "unlock"; never unlock on it
    if best[0] in ("wifi_on", "wifi_off"):             # the two differ by one word, so decide on that word alone
        words = set(s.split())
        if not words & {"wifi", "wi", "fi", "wireless", "network", "internet", "offline", "online"}:
            best = (None, 0.0)                           # "turn off the screen" is not a Wi-Fi command
        else:
            best = (("wifi_off", best[1]) if words & {"off", "disable", "offline"} else
                    ("wifi_on", best[1]) if words & {"on", "enable", "online"} else (None, 0.0))
    if best[0] == "sleep" and not ({"sleep", "lock", "display"} & set(s.split())):
        best = (None, 0.0)                       # whole words only, so "clock" or "asleep" never lock the screen
    if best[1] >= cutoff:
        return (best[0], {})
    app, r = best_app(s)
    return ("open", {"name": app}) if r >= 0.86 else None


def execute(intent, args, cfg):
    """Run a matched command. Returns the sentence to speak."""
    if intent == "open":
        r = tool("open_app", args)
        return f"Opening {args['name']}" if r.get("ok") else f"Could not open {args['name']}"
    if intent == "unlock":
        st = tool("unlock_screen")
        return "" if st.get("screen") not in ("locked", "asleep") else "Could not unlock"
    if intent in ("wifi_on", "wifi_off"):
        r = tool("wifi_power", {"on": intent == "wifi_on"})
        if not r.get("ok"):
            return "Could not change Wi-Fi"
        play(intent)                                   # rising tones = on, falling = off; speech is not available offline
        return ""
    if intent == "sleep":
        r = tool("sleep_screen")
        return "" if r.get("ok") else "Could not sleep, " + (r.get("reason") or "the screen stayed on")
    if intent == "home":
        return "Home" if tool("go_home").get("ok") else "Could not go home"
    if intent in ("volume_up", "volume_down"):
        cur = tool("get_volume").get("percent") or 50
        return "Volume %s" % tool("set_volume", {"percent": cur + (15 if intent.endswith("up") else -15)}).get("percent")
    if intent == "mute":
        tool("set_volume", {"percent": 0})
        return ""
    if intent == "set_volume":
        return "Volume %s" % tool("set_volume", args).get("percent")
    if intent in ("brightness_up", "brightness_down"):
        cur = tool("get_brightness").get("percent") or 50
        new = max(5, cur + (25 if intent.endswith("up") else -25))
        return "Brightness %s" % tool("set_brightness", {"percent": new}).get("percent")
    if intent == "set_brightness":
        return "Brightness %s" % tool("set_brightness", {"percent": max(5, args["percent"])}).get("percent")
    if intent == "battery":
        b = tool("battery")
        return f"Battery {b.get('percent')} percent, {str(b.get('status')).lower()}"
    if intent == "temperature":
        return "%s degrees" % round(tool("system_info").get("temperature_c", 0))
    if intent == "wifi":
        w = tool("wifi_status")
        if not w.get("connected"):
            return "Wi-Fi is not connected"
        return f"Connected to {w['ssid']}, signal {w['signal_percent']} percent, address {' dot '.join(w['ip'][0].split('.'))}"
    if intent == "status":
        s = tool("system_info")
        return f"{round(s['temperature_c'])} degrees, {s['memory_mb']['available']} megabytes free, load {s['load_1m_5m_15m'][0]}"
    if intent == "time":
        return time.strftime("%-I %M %p")
    if intent == "wake":
        saved = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        saved.setdefault("wake", {})["models"] = [args["model"]]
        CONFIG.write_text(json.dumps(saved, indent=1))
        return "Wake word is now " + args["model"].split("_v")[0].replace("_", " ")
    return ""


# ------------------------------------------------------------------ speech recognition

def load_asr(cfg):
    """Moonshine tiny (non-streaming). Chosen after the streaming zipformer model returned nothing useful on
    short commands; Moonshine got the same real clip right. See the project notes for the comparison."""
    import sherpa_onnx
    m = str(BASE / "asr" / "sherpa-onnx-moonshine-tiny-en-int8")
    return sherpa_onnx.OfflineRecognizer.from_moonshine(
        preprocessor=f"{m}/preprocess.onnx", encoder=f"{m}/encode.int8.onnx",
        uncached_decoder=f"{m}/uncached_decode.int8.onnx", cached_decoder=f"{m}/cached_decode.int8.onnx",
        tokens=f"{m}/tokens.txt", num_threads=2)


def transcribe(asr, pcm):
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    if len(x) < RATE // 4:
        return ""
    peak = float(np.abs(x).max())
    if peak > 0:
        x = x * min(40.0, 0.5 / peak)          # the mic arrives very quiet; bring it up to a normal level
    s = asr.create_stream()
    s.accept_waveform(RATE, x)
    asr.decode_stream(s)
    return s.result.text.strip()


def wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    return buf.getvalue()


def boost(pcm):
    """The mic arrives about 40 dB below full scale; raise it before sending it anywhere."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    return pcm if peak <= 0 else (x * min(40.0, 16000.0 / peak)).astype(np.int16).tobytes()


def server_transcribe(pcm, cfg):
    d, b = cfg["dictation"], uuid.uuid4().hex
    field = lambda k, v: f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body = b"".join([field("model", d["model"]), field("language", "en"),
                     f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                     f'Content-Type: audio/wav\r\n\r\n'.encode(), wav_bytes(boost(pcm)), f"\r\n--{b}--\r\n".encode()])
    req = urllib.request.Request(d["url"], body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("text", "").strip()


# ------------------------------------------------------------------ handlers (run off the audio thread)

def handle_command(text, pcm, source, cfg):
    try:
        if not text:
            play("empty")
            ov("none", "Didn't catch that", 1.2)
            log("command", source=source, text="", result="nothing heard")
            return
        hit = match(text, cfg["voice"]["match_cutoff"])
        ov("heard", text)                        # what was transcribed, in white
        if hit:
            play("ok")
            time.sleep(0.3 if OV else 0)
            show_match(hit[0], hit[1], text, cfg)        # the same text turns green
            spoken = execute(hit[0], hit[1], cfg)
            show_result(hit[0], spoken)
            aloud = confirm(hit[0], spoken, cfg)
            log("command", source=source, text=text, intent=hit[0], args=hit[1], result=spoken, spoken_aloud=aloud)
        else:                                    # not a device command: hand the audio to xiaozhi
            wav = LOGS / "last_unmatched.wav"
            wav.write_bytes(wav_bytes(pcm))
            play("dictate")
            ov("ask", "xiaozhi: " + text, 3.0)
            hook = cfg["voice"]["on_unmatched"] or [sys.executable, str(BASE / "xz_handoff.py")]
            r = subprocess.run(list(hook) + [str(wav), text], cwd=str(BASE), capture_output=True, text=True, timeout=90)
            last = (r.stdout.strip().splitlines() or [""])[-1][:240]
            if r.returncode != 0:
                play("nomatch")
            log("command", source=source, text=text, result="handed to xiaozhi", exit=r.returncode, reply=last)
    except Exception as e:
        play("error")
        ov("error", "Something went wrong", 2.0)
        log("error", where="command", text=text, error=f"{type(e).__name__}: {e}")


def handle_dictation(text, pcm, cfg):
    try:
        engine = cfg["dictation"]["engine"]
        if engine == "server" or not text:
            text = server_transcribe(pcm, cfg)
        if text:
            tool("type_text", {"text": text + " "})
        play("ok" if text else "empty")
        ov("ok", text, 1.6) if text else ov("none", "Didn't catch that", 1.2)
        log("dictation", engine=engine, text=text)
    except Exception as e:
        play("error")
        ov("error", "Dictation failed", 2.0)
        log("error", where="dictation", error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ keys

class Keys(threading.Thread):
    """Watches the keyboard passively. tab_fired: TAB held long enough. alt_down: ALT held alone."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg, self.tab_fired, self.alt_down = cfg, threading.Event(), threading.Event()
        self.down = {}

    def run(self):
        fd = os.open(KBD, os.O_RDONLY)
        threading.Thread(target=self.poll, daemon=True).start()
        while True:
            data = os.read(fd, 24 * 32)
            for i in range(0, len(data) - 23, 24):
                _, _, typ, code, val = struct.unpack("llHHi", data[i:i + 24])
                if typ != 1 or val == 2:
                    continue
                if val == 1:
                    self.down[code] = time.monotonic()
                    if code != KEY_ALT:
                        self.down.pop(KEY_ALT, None)       # ALT used as a normal modifier: not dictation
                else:
                    self.down.pop(code, None)
                    if code == KEY_ALT:
                        self.alt_down.clear()

    def poll(self):
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            t = self.down.get(KEY_TAB)
            if t and now - t >= self.cfg["voice"]["tab_hold_s"] and len(self.down) == 1:
                self.down[KEY_TAB] = now + 3600            # fire once per hold
                self.tab_fired.set()
            a = self.down.get(KEY_ALT)
            if a and now - a >= self.cfg["dictation"]["hold_s"] and len(self.down) == 1:
                self.alt_down.set()


# ------------------------------------------------------------------ mic sharing

class MicWatch(threading.Thread):
    """True in .busy while another program is recording through PipeWire (Recorder, Camera video, a voice
    app). The wake word is paused then, so it cannot fire into someone else's recording."""

    def __init__(self):
        super().__init__(daemon=True)
        self.busy = False

    def others(self):
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5).stdout
        names = []
        for block in out.split("	id ")[1:]:
            if 'media.class = "Stream/Input/Audio"' in block:
                m = re.search(r'node.name = "([^"]*)"', block)
                if m and m.group(1) != "pw-cat":
                    names.append(m.group(1))
        return names

    def run(self):
        while True:
            try:
                names = self.others()
                if bool(names) != self.busy:
                    self.busy = bool(names)
                    log("mic", wake_word="paused" if names else "listening", other_apps=names)
            except Exception:
                pass
            time.sleep(3)


# ------------------------------------------------------------------ hand-over to another app

def pause_path():
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "cz-voice.pause"


def pause_active():
    """True while an app (TARS remote) holds the pause flag: a file with its PID, and that PID is alive.
    A missing file, unreadable content or a dead PID means the flag is stale and is ignored."""
    try:
        pid = int(pause_path().read_text().split()[0])
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except PermissionError:
        return True                                        # alive, owned by someone else
    except (OSError, ValueError, IndexError, OverflowError):
        return False
    return True


# ------------------------------------------------------------------ main loop

def chunk_rms(pcm):
    return float(np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2)))


def main():
    global OV
    from collections import deque
    from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
    cfg = load_config()
    make_sounds()
    local = "local" in (cfg["voice"]["engine"], cfg["dictation"]["engine"])
    asr = load_asr(cfg) if local else None      # the on-device model costs ~160 MB, so it is off by default
    log("start", asr="moonshine-tiny (on device)" if local else "server", commands=cfg["voice"]["engine"], keywords="on device", wake=cfg["wake"]["models"], dictation=cfg["dictation"]["engine"])

    def load_wake():
        out = []
        for name in cfg["wake"]["models"]:
            p = BASE / "models" / f"{name}.json"
            if p.exists():
                m = MicroWakeWord.from_config(p)
                if cfg["wake"]["cutoff"] is not None:
                    m.probability_cutoff = cfg["wake"]["cutoff"]
                out.append((name, m))
        return out

    try:
        import overlay
        OV = overlay.Overlay(enabled=bool(cfg["voice"].get("overlay", True)))
    except Exception as e:
        log("error", where="overlay load", error=f"{type(e).__name__}: {e}")
    spotter = None                               # small on-device recogniser for the fixed commands

    def load_spotter():                          # takes about 10 s, so it loads in the background: the wake word and
        nonlocal spotter                         # the server path work meanwhile (matters after every return from an app)
        try:
            import kws_commands
            sp = kws_commands.CommandSpotter(str(BASE / "kws"), str(BASE / "kws" / "keywords.txt"))
            spotter = sp
            log("keywords", state="ready")
        except Exception as e:
            log("error", where="keyword spotter load", error=f"{type(e).__name__}: {e}")
    if cfg["voice"].get("kws", True):
        threading.Thread(target=load_spotter, daemon=True).start()

    def on_term(*_):                             # stopped by systemd (for example when an app opens): leave the screen clean
        ov_hide()
        os._exit(0)
    import signal
    signal.signal(signal.SIGTERM, on_term)
    wake, feats = load_wake(), MicroWakeWordFeatures()
    keys = Keys(cfg)
    keys.start()
    mic = MicWatch()
    mic.start()
    def start_rec():
        return subprocess.Popen(["pw-cat", "--record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                                 "--latency", "20ms", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rec = start_rec()
    paused, last_pause = False, 0.0
    st = {"state": "idle", "skip": 0, "buf": bytearray(), "started": 0.0, "source": "",
          "floor": 50.0, "speech": 0, "quiet": 0}
    recent = deque(maxlen=100)                             # last 2 s of loudness, for the noise floor
    busy = threading.Event()
    cfg_mtime = CONFIG.stat().st_mtime if CONFIG.exists() else 0
    last_check = time.monotonic()

    def begin(state, source, sound, skip):
        play(sound)
        ov("listen", "Listening" if state == "command" else "Dictating")
        floor = float(np.percentile(recent, 20)) if len(recent) > 10 else 50.0
        st.update(state=state, source=source, skip=skip, buf=bytearray(), started=time.monotonic(),
                  floor=floor, speech=0, quiet=0)
        st["kws"] = bool(spotter) and state == "command"     # only if it had finished loading when listening began
        if st["kws"]:
            spotter.start()

    def run_after(fn, *a):
        busy.set()
        st["state"] = "idle"

        def work():
            try:
                fn(*a)
            finally:
                for _, m in wake:
                    m.reset()
                feats.reset()
                keys.tab_fired.clear()
                busy.clear()
        threading.Thread(target=work, daemon=True).start()

    def command_done(heard_speech):
        pcm, src = bytes(st["buf"]), st["source"]
        if heard_speech:
            ov("work", "Transcribing")

        def job():
            try:
                if not heard_speech:
                    text = ""
                elif cfg["voice"]["engine"] == "local" and asr:
                    text = transcribe(asr, pcm)
                else:
                    text = server_transcribe(pcm, cfg)
            except Exception as e:
                play("error")
                ov("error", "Can't reach the server", 2.5)
                log("error", where="recognition", error=f"{type(e).__name__}: {e}")
                return
            handle_command(text, pcm, src, cfg)
        run_after(job)

    def keyword_hit(label):
        src = st["source"]

        def job():
            intent, args = ("open", {"name": label.split(":", 1)[1]}) if label.startswith("open:") else (label, {})
            try:
                play("ok")
                show_match(intent, args, "", cfg)
                spoken = execute(intent, args, cfg)
                show_result(intent, spoken)
                aloud = confirm(intent, spoken, cfg)
                log("command", source=src, via="keyword spotter (on device)", intent=intent, args=args, result=spoken, spoken_aloud=aloud)
            except Exception as e:
                play("error")
                ov("error", "Something went wrong", 2.0)
                log("error", where="keyword command", error=f"{type(e).__name__}: {e}")
        run_after(job)

    def dictation_done():
        pcm = bytes(st["buf"])
        ov("work", "Transcribing")

        def job():
            text = transcribe(asr, pcm) if cfg["dictation"]["engine"] == "local" and asr else ""
            handle_dictation(text, pcm, cfg)
        run_after(job)

    while True:
        if time.monotonic() - last_pause >= 1:             # another app (TARS remote) may need the mic
            last_pause = time.monotonic()
            want = pause_active()
            if want and not paused:
                paused = True
                rec.terminate()
                rec.wait()
                st.update(state="idle", skip=0, buf=bytearray())
                ov_hide()
                log("paused")
            elif paused and not want:
                paused = False
                rec = start_rec()
                recent.clear()
                for _, m in wake:
                    m.reset()
                feats.reset()
                keys.tab_fired.clear()
                log("resumed")
        if paused:
            keys.tab_fired.clear()
            time.sleep(0.2)
            continue
        pcm = rec.stdout.read(CHUNK)
        if not pcm:
            sys.exit("audio capture ended")
        now = time.monotonic()
        if now - last_check > 2:                           # pick up changes made by tools (wake word, engine)
            last_check = now
            mt = CONFIG.stat().st_mtime if CONFIG.exists() else 0
            if mt != cfg_mtime:
                cfg_mtime = mt
                cfg.clear()
                cfg.update(load_config())
                wake = load_wake()
                log("config", wake=cfg["wake"]["models"], dictation=cfg["dictation"]["engine"])
        if busy.is_set():
            continue
        if st["state"] == "idle":
            recent.append(chunk_rms(pcm))
            if keys.alt_down.is_set():
                begin("dictate", "alt", "dictate", 4)
            elif keys.tab_fired.is_set():
                keys.tab_fired.clear()
                begin("command", "tab", "wake", 15)
            elif cfg["wake"]["enabled"] and not mic.busy:
                for f in feats.process_streaming(pcm):
                    if any([m.process_streaming(f) for _, m in wake]):
                        begin("command", "wake", "wake", 15)
                        break
            continue
        if st["skip"]:                                     # let the cue sound finish before listening
            st["skip"] -= 1
            continue
        st["buf"] += pcm
        if st["state"] == "command":
            label = spotter.feed(pcm) if st.get("kws") else None
            if label:                                      # fixed command recognised on the device: act now
                keyword_hit(label)
                continue
            rms = chunk_rms(pcm)
            loud = rms > max(st["floor"] * 2.2, st["floor"] + 40)
            if OV:
                OV.level(rms / max(st["floor"], 1.0))
            st["speech"] += loud
            st["quiet"] = 0 if loud else st["quiet"] + 1
            elapsed = now - st["started"]
            spoke = st["speech"] >= 6                      # at least ~120 ms of sound above the room noise
            if (spoke and st["quiet"] * 0.02 >= cfg["voice"]["silence_s"]) or (not spoke and elapsed > 4.5) \
                    or elapsed > cfg["voice"]["max_utterance_s"] + 1:
                command_done(spoke)
        elif st["state"] == "dictate":
            if OV:
                OV.level(chunk_rms(pcm) / max(st["floor"], 1.0))
            if not keys.alt_down.is_set() or now - st["started"] > cfg["dictation"]["max_s"]:
                dictation_done()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--text":       # matcher test: voice.py --text "open the camera" [--run]
        cfg = load_config()
        hit = match(sys.argv[2], cfg["voice"]["match_cutoff"])
        print(f"{sys.argv[2]!r:42} -> {hit}")
        if hit and "--run" in sys.argv:
            print("said:", execute(hit[0], hit[1], cfg))
    elif len(sys.argv) > 2 and sys.argv[1] == "--wav":      # recognition + matcher on a 16 kHz mono wav
        cfg = load_config()
        asr = load_asr(cfg)
        w = wave.open(sys.argv[2])
        pcm = w.readframes(w.getnframes())
        t0 = time.time()
        text = transcribe(asr, pcm)
        print(f"heard {text!r} in {time.time() - t0:.1f}s ({len(pcm) / 2 / RATE:.1f}s audio) -> "
              f"{match(text, cfg['voice']['match_cutoff'])}")
    else:
        main()
