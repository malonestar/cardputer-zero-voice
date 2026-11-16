#!/usr/bin/env python3
"""TARS remote: push-to-talk / typed remote for the TARS robot, for the M5Stack CardputerZero (320x170).

The robot speaks the replies itself. This app records (or takes typed text), transcribes on the home server,
POSTs the text to the robot and shows the conversation. After the robot finishes speaking (bot_audio_done) it
opens the mic once more for a hands-free follow-up. Protocol: docs/TARS_REMOTE_SPEC.md.

Hardware sits behind small backends (display, keys, audio in, speech-to-text, tones) so everything can run
headless: see --help.
"""
import argparse, io, json, math, os, queue, re, signal, struct, subprocess, sys, threading, time, urllib.request, uuid, wave
from array import array
from collections import deque
from pathlib import Path

W, H = 320, 170
RATE, CHUNK = 16000, 640                 # CHUNK bytes = 320 samples = 20 ms, as in voice.py
CHUNK_S = 0.02
KBD = "/dev/input/by-path/platform-3f804000.i2c-event"
FB = "/dev/fb0"
PTT_MAX_S = 30.0                          # safety cap for a held talk key
PTT_MIN_S = 0.25

DEFAULTS = {
    "url": "http://YOUR-TARS-ROBOT",
    "password_file": "~/cz-ai/tars_password",
    "talk_key": "tab",
    "follow_up": True,
    "follow_up_wait_s": 5,
    "follow_up_settle_s": 0.7,            # after TARS stops talking, sound this early is not counted as speech
    "silence_s": 0.9,
    "max_utterance_s": 12,
    "turn_timeout_s": 60,
    "stt_url": "http://YOUR-SPEECH-SERVER:8001/v1/audio/transcriptions",
    "stt_model": "Systran/faster-whisper-small",
}

# key codes and the shifted symbols, from cz_tools.py
KEYS = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "enter": 28, "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "'": 40, "ctrl": 29, "shift": 42, "alt": 56, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, ".": 52, "/": 53, "space": 57, " ": 57, "up": 103, "left": 105, "right": 106, "down": 108,
}
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/"}
K_ESC, K_TAB, K_ENTER, K_BS = 1, 15, 28, 14
K_SHIFTS = (42, 54)                      # 54 (right shift) is not in cz_tools' map; harmless if it never occurs
K_UP, K_DOWN = 103, 108
CHAR = {}                                 # key code -> (plain, shifted)
for _n, _c in KEYS.items():
    if len(_n) == 1 and _n != " ":
        CHAR[_c] = (_n, _n.upper() if _n.isalpha() else next((s for s, b in SHIFTED.items() if b == _n), _n))
CHAR[57] = (" ", " ")
KEYNAME = {v: k for k, v in KEYS.items() if k != " "}

STATES = {   # label, colour (pill background)
    "connecting": ("Connecting", (150, 160, 175)),
    "ready": ("Ready", (70, 210, 110)),
    "listening": ("Listening", (255, 176, 32)),
    "transcribing": ("Transcribing", (60, 200, 220)),
    "thinking": ("Thinking", (180, 140, 255)),
    "speaking": ("Speaking", (90, 160, 255)),
    "offline": ("Offline", (240, 85, 85)),
}
BG, BAR_BG, FG, DIM = (14, 16, 22), (30, 33, 44), (225, 228, 235), (130, 136, 150)
C_YOU, C_TARS, C_WARN = (110, 190, 255), (255, 190, 70), (255, 105, 90)

TONE_FOR = {"ptt": "dictate", "followup": "wake", "ok": "ok", "error": "error", "empty": "empty"}


def load_config(path, overrides=None):
    cfg = dict(DEFAULTS)
    try:
        sec = json.loads(Path(path).read_text()).get("tars", {})
        if isinstance(sec, dict):
            cfg.update({k: v for k, v in sec.items() if k in DEFAULTS})
    except Exception:
        pass
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


# ------------------------------------------------------------------ small helpers (copied from voice.py)

def wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    return buf.getvalue()


def boost(pcm):
    """The mic arrives about 40 dB below full scale; raise it before sending it anywhere. Same arithmetic as
    voice.py's boost(), written with array instead of numpy to keep the app's memory small."""
    a = array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    peak = max(max(a), -min(a)) if a else 0
    if peak <= 0:
        return pcm
    g = min(40.0, 16000.0 / peak)
    return array("h", [int(v * g) for v in a]).tobytes()


def chunk_rms(pcm):
    a = array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def server_transcribe(pcm, url, model):
    b = uuid.uuid4().hex
    field = lambda k, v: f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body = b"".join([field("model", model), field("language", "en"),
                     f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                     f'Content-Type: audio/wav\r\n\r\n'.encode(), wav_bytes(boost(pcm)), f"\r\n--{b}--\r\n".encode()])
    req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("text", "").strip()


def clean_text(t):
    return "".join(ch for ch in str(t) if ch == "\n" or (ch >= " " and ord(ch) <= 0xFFFF and ch != "\x7f"))


# ------------------------------------------------------------------ backends: display

def rgb565(img):
    """RGB image -> little-endian RGB565 bytes (what Pillow's 'BGR;16' raw decoder reads), without numpy."""
    from PIL import ImageChops
    r, g, b = img.convert("RGB").split()
    low = ImageChops.add(g.point(lambda v: (v & 0x1C) << 3), b.point(lambda v: v >> 3))
    high = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
    out = bytearray(img.width * img.height * 2)
    out[0::2] = low.tobytes()
    out[1::2] = high.tobytes()
    return bytes(out)


class FbDisplay:
    def __init__(self, path=FB):
        self.fd = os.open(path, os.O_WRONLY)

    def show(self, img):
        os.pwrite(self.fd, rgb565(img), 0)

    def close(self):
        os.close(self.fd)


class PngDisplay:
    def __init__(self, folder, scale=1):
        self.dir, self.scale, self.n, self.last = Path(folder), scale, 0, None
        self.dir.mkdir(parents=True, exist_ok=True)

    def show(self, img):
        raw = img.tobytes()
        if raw == self.last:
            return
        self.last, self.n = raw, self.n + 1
        out = img if self.scale == 1 else img.resize((W * self.scale, H * self.scale))
        out.save(self.dir / f"frame_{self.n:05d}.png")
        out.save(self.dir / "latest.png")

    def close(self):
        pass


# ------------------------------------------------------------------ backends: keys

class EvdevKeys(threading.Thread):
    """Reads the keyboard device passively (not grabbed). Posts ('key', code, value)."""

    def __init__(self, q, path=KBD):
        super().__init__(daemon=True)
        self.q, self.fd = q, os.open(path, os.O_RDONLY)

    def run(self):
        st = struct.Struct("llHHi")
        while True:
            try:
                data = os.read(self.fd, st.size * 32)
            except OSError:
                return
            for i in range(0, len(data) - st.size + 1, st.size):
                _, _, typ, code, val = st.unpack(data[i:i + st.size])
                if typ == 1:
                    self.q.put(("key", code, val))


def parse_script(text):
    """Script lines (times are seconds since start):
         T press KEY [HOLD_S]     key down at T, up after HOLD_S (default 0.05)
         T hold KEY HOLD_S        same, hold is required
         T down KEY | T up KEY
         T type some text         types the characters (shift used for capitals and symbols)"""
    ev = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].rstrip()
        if not ln.strip():
            continue
        t, cmd, *rest = ln.split(None, 2)
        t = float(t)
        arg = rest[0] if rest else ""
        if cmd in ("press", "hold"):
            name, *h = arg.split()
            hold = float(h[0]) if h else 0.05
            ev += [(t, KEYS[name], 1), (t + hold, KEYS[name], 0)]
        elif cmd in ("down", "up"):
            ev.append((t, KEYS[arg.strip()], 1 if cmd == "down" else 0))
        elif cmd == "type":
            text_, tt = ln.split(None, 2)[2], t
            for ch in text_:
                low = SHIFTED.get(ch) or (ch.lower() if ch.isalpha() else ch)
                if low not in KEYS:
                    continue
                shift = ch in SHIFTED or ch.isupper()
                if shift:
                    ev.append((tt, 42, 1)); tt += 0.01
                ev += [(tt, KEYS[low], 1), (tt + 0.02, KEYS[low], 0)]
                tt += 0.04
                if shift:
                    ev.append((tt, 42, 0)); tt += 0.01
        else:
            raise ValueError("bad script line: " + ln)
    return sorted(ev, key=lambda e: e[0])


class ScriptKeys(threading.Thread):
    def __init__(self, q, path):
        super().__init__(daemon=True)
        self.q, self.ev = q, parse_script(Path(path).read_text())

    def run(self):
        t0 = time.monotonic()
        for t, code, val in self.ev:
            d = t0 + t - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.q.put(("key", code, val))


# ------------------------------------------------------------------ backends: audio in

class PwSession:
    """One pw-cat recorder, same command line as voice.py."""

    def __init__(self):
        self.p = subprocess.Popen(["pw-cat", "--record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                                   "--latency", "20ms", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self):
        return self.p.stdout.read(CHUNK)

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(2)
        except Exception:
            self.p.kill()


class PwMic:
    def open(self):
        return PwSession()


class WavSession:
    def __init__(self, pcm, speed, noise, seed):
        self.pcm, self.speed, self.noise, self.rng = pcm, speed, noise, seed
        self.n, self.t0, self.closed = 0, time.monotonic(), threading.Event()

    def read(self):
        due = self.t0 + self.n * CHUNK_S / self.speed
        while not self.closed.is_set() and time.monotonic() < due:
            time.sleep(min(0.005, max(0.0, due - time.monotonic())))
        if self.closed.is_set():
            return b""
        c = self.pcm[self.n * CHUNK:(self.n + 1) * CHUNK]
        self.n += 1
        if len(c) < CHUNK:
            a = array("h")
            for _ in range(CHUNK // 2 - len(c) // 2):
                self.rng = (self.rng * 1103515245 + 12345) & 0x7FFFFFFF
                a.append((self.rng >> 8) % (2 * self.noise + 1) - self.noise)
            c = c + a.tobytes()
        return c

    def close(self):
        self.closed.set()


class WavMic:
    """Plays 16 kHz mono s16 WAV files as if they were the mic. Every open() starts the next file; when the
    files run out (or a file ends) the mic carries low room noise. speed > 1 runs faster than real time."""

    def __init__(self, files, speed=1.0, noise=20):
        self.files, self.speed, self.noise, self.i = list(files), speed, noise, 0

    def open(self):
        pcm = b""
        if self.i < len(self.files):
            with wave.open(self.files[self.i]) as w:
                assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 16k mono s16"
                pcm = w.readframes(w.getnframes())
        self.i += 1
        return WavSession(pcm, self.speed, self.noise, 12345 + self.i)


# ------------------------------------------------------------------ the app

class Tars:
    def __init__(self, cfg, a, display, mic, stt, keysrc_factory):
        self.cfg, self.a, self.display, self.mic, self.stt = cfg, a, display, mic, stt
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.exit_reason = "quit"
        self.keysrc_factory = keysrc_factory
        self.base = cfg["url"].rstrip("/")
        self.talk_code = KEYS.get(str(cfg["talk_key"]).lower(), K_TAB)
        self.talk_name = str(cfg["talk_key"]).upper()
        self.fonts = load_fonts()
        self.t_start = time.monotonic()
        # net
        self.state = "connecting"
        self.connected = False
        self.problem = None                  # (title, [lines]) shown in the conversation area
        self.retry_at = 0.0
        self.sio = None
        self.http = None
        self.metrics = ""
        # conversation
        self.lines = []                      # [{"who": "you"|"tars"|"warn"|"sys", "text": str}]
        self.cur_tars = None                 # the TARS line currently being streamed
        self.scroll = 0
        self.last_total = 0
        self.input = ""
        self.shift = False
        self.notice, self.notice_until = "", 0.0
        # turn
        self.turn = None                     # {"ours": bool, "t": float, "phase": "thinking"|"speaking"}
        # listening
        self.listen = None
        self.gen = 0
        self.recent = deque(maxlen=100)
        self.level = 0.0
        self.dirty = True
        self.last_draw = 0.0
        self.pause_file = None

    # ---- logging / state
    def log(self, event, **kw):
        rec = {"ts": round(time.time(), 3), "event": event, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        if self.a.event_log:
            with self.a.event_log_lock, open(self.a.event_log, "a") as f:
                f.write(line + "\n")
        if self.a.verbose:
            print(line, file=sys.stderr, flush=True)

    def set_state(self, s):
        if s != self.state:
            self.state = s
            self.log("state", state=s)
            self.dirty = True

    def note(self, text, secs=3.0):
        self.notice, self.notice_until = text, time.monotonic() + secs
        self.log("notice", text=text)
        self.dirty = True

    def add_line(self, who, text):
        self.lines.append({"who": who, "text": clean_text(text)})
        self.lines = self.lines[-200:]
        self.dirty = True
        return self.lines[-1]

    # ---- pause flag (hand-over of the mic from voice.py)
    def write_flag(self):
        p = self.pause_file
        tmp = p.with_name(p.name + f".{os.getpid()}")
        try:
            tmp.write_text(f"{os.getpid()}\n")
            os.replace(tmp, p)
        except OSError as e:                                 # no runtime dir: the voice layer cannot be paused
            self.log("flag_failed", path=str(p), error=str(e))
            return
        self.log("flag_written", path=str(p), pid=os.getpid())

    def remove_flag(self):
        p = self.pause_file
        try:
            if p and p.exists() and p.read_text().split()[:1] == [str(os.getpid())]:
                p.unlink()
                self.log("flag_removed")
        except Exception:
            pass

    # ---- network supervisor (own thread)
    def password(self):
        path = Path(os.path.expanduser(self.a.password_file or self.cfg["password_file"]))
        try:
            pw = path.read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            pw = ""
        if not pw:
            return None, path
        return pw, path

    def net_thread(self):
        import requests
        self.http = requests.Session()
        last_pw, wrong_at = None, 0.0
        while not self.stop.is_set():
            self.connected = False
            pw, path = self.password()
            if pw is None:
                self.q.put(("problem", "No password file", [f"Create {self.shown_path(path)}", "with the robot web UI password", "(first line)"], "offline"))
                self.log("password_missing", path=str(path))
                self.stop.wait(self.a.retry_s)
                continue
            if pw == last_pw and time.monotonic() - wrong_at < 30:      # wrong password: only retry on change
                self.stop.wait(1)
                continue
            try:
                r = self.http.get(self.base + "/login", timeout=3, allow_redirects=False)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
            except Exception as e:
                self.offline(e)
                continue
            try:
                ok = self.login(pw)
            except Exception as e:
                self.offline(e)
                continue
            if not ok:
                last_pw, wrong_at = pw, time.monotonic()
                self.q.put(("problem", "Wrong password", [f"TARS refused the password in", self.shown_path(path)], "offline"))
                self.log("login_failed", reason="wrong password")
                continue
            last_pw = None
            if not self.connect_socket():
                self.offline("socket.io connect failed")
                continue
            self.connected = True
            self.q.put(("online",))
            last_metrics = 0.0
            while not self.stop.is_set() and self.sio.connected:
                if time.monotonic() - last_metrics > 30:
                    last_metrics = time.monotonic()
                    self.fetch_metrics()
                self.stop.wait(1)
            self.connected = False
            try:
                self.sio.disconnect()
            except Exception:
                pass
            if not self.stop.is_set():
                self.q.put(("lost",))
                self.log("socket_lost")
                self.stop.wait(self.a.retry_s)

    @staticmethod
    def shown_path(p):
        home = str(Path.home())
        s = str(p)
        return "~" + s[len(home):] if s.startswith(home) else s

    def offline(self, err):
        self.q.put(("problem", "TARS is not answering", [self.base.replace("http://", ""), f"retrying every {self.a.retry_s:g} s"], "offline"))
        self.log("robot_offline", error=str(err)[:120])
        self.stop.wait(self.a.retry_s)

    def login(self, pw):
        r = self.http.post(self.base + "/login", data={"password": pw}, timeout=5, allow_redirects=False)
        ok = r.status_code in (301, 302, 303) and "login" not in r.headers.get("Location", "")
        if ok:
            self.log("login_ok")
        return ok

    def connect_socket(self):
        import socketio
        for transports in (["websocket"], ["polling"]):
            sio = socketio.Client(reconnection=False)
            self.register(sio)
            try:
                sio.connect(self.base, transports=transports, wait_timeout=5)
                self.sio = sio
                self.log("socket_connected", transport=transports[0])
                return True
            except Exception as e:
                self.log("socket_failed", transport=transports[0], error=str(e)[:120])
                try:
                    sio.disconnect()
                except Exception:
                    pass
        return False

    def register(self, sio):
        for name in ("bot_stream_start", "bot_token", "talking_state", "bot_message", "bot_audio_done"):
            sio.on(name, (lambda n: lambda data=None: self.q.put(("sio", n, data)))(name))

    def fetch_metrics(self):
        try:
            r = self.http.get(self.base + "/api/system/metrics", timeout=4, allow_redirects=False)
            if r.status_code != 200:
                return
            self.q.put(("metrics", parse_metrics(r.json())))
        except Exception:
            pass

    def send_thread(self, text, source):
        try:
            for attempt in (1, 2):
                r = self.http.post(self.base + "/process_llm", data={"message": text, "speaker": "robot"},
                                   timeout=10, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308) and "login" in r.headers.get("Location", ""):
                    pw, _ = self.password()
                    self.log("relogin", on="process_llm")
                    if attempt == 1 and pw and self.login(pw):
                        continue
                    raise RuntimeError("login required")
                if r.status_code >= 400 or r.json().get("status") != "success":
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:60]}")
                self.q.put(("sent", text))
                return
        except Exception as e:
            self.q.put(("send_failed", text, str(e)[:100]))

    # ---- sending a message
    def can_send(self):
        return (self.connected and self.state not in ("offline", "connecting", "transcribing")
                and self.turn is None and not self.listen)

    def send(self, text, source):
        text = clean_text(text).strip()
        if not text:
            return
        self.log("send", text=text, source=source)
        self.add_line("you", text)
        self.scroll = 0
        self.turn = {"ours": True, "t": time.monotonic(), "phase": "thinking"}
        self.cur_tars = None
        self.set_state("thinking")
        threading.Thread(target=self.send_thread, args=(text, source), daemon=True).start()

    def end_turn(self, why):
        ours = bool(self.turn and self.turn["ours"])
        self.turn = None
        self.cur_tars = None
        self.set_state("ready" if self.connected else "offline")
        self.log("turn_end", why=why, ours=ours)
        return ours

    # ---- listening
    def start_listen(self, mode):
        self.gen += 1
        tone = TONE_FOR[mode]
        self.listen = {"mode": mode, "gen": self.gen, "buf": bytearray(), "speech": 0, "quiet": 0, "n": 0,
                       "skip": 4 if mode == "ptt" else 15, "released": False}
        if not self.a.sound:
            self.listen["skip"] = 0
        self.play(tone)
        self.level = 0.0
        self.set_state("listening")
        self.log("listen_start", mode=mode)
        self.listen["t_open"] = time.monotonic()
        try:
            self.listen["sess"] = sess = self.mic.open()
        except Exception as e:
            self.listen = None
            self.note("Mic error: " + str(e)[:40])
            self.set_state("ready")
            return
        threading.Thread(target=self.capture, args=(self.gen, sess), daemon=True).start()

    def capture(self, gen, sess):
        while gen == self.gen:
            try:
                d = sess.read()
            except Exception:
                d = b""
            if not d:
                break
            self.q.put(("audio", gen, d))

    def floor(self):
        r = sorted(self.recent)
        return r[int(0.2 * (len(r) - 1))] if len(r) > 10 else 50.0

    def on_audio(self, gen, pcm):
        L = self.listen
        if not L or L["gen"] != gen:
            return
        if L["skip"]:
            L["skip"] -= 1
            return
        L["buf"] += pcm
        L["n"] += 1
        rms = chunk_rms(pcm)
        fl = self.floor()
        thr = max(fl * 2.2, fl + 40)
        loud = rms > thr
        if not loud:
            self.recent.append(rms)
        # a follow-up opens the instant TARS stops: its voice tail and our own tone must not count as speech
        settling = L["mode"] != "ptt" and L["n"] * CHUNK_S <= self.cfg["follow_up_settle_s"]
        L["speech"] += loud and not settling
        L["quiet"] = 0 if loud else L["quiet"] + 1
        L["thr"], L["fl"] = thr, fl
        self.level = rms
        self.dirty = True
        elapsed = L["n"] * CHUNK_S
        spoke = L["speech"] >= 6
        if L["mode"] == "ptt":
            if elapsed > PTT_MAX_S:
                self.finish_listen("max length")
            return
        if spoke and L["quiet"] * CHUNK_S >= self.cfg["silence_s"]:
            self.finish_listen("end of speech")
        elif not spoke and elapsed > self.cfg["follow_up_wait_s"]:
            self.log("followup_timeout")
            self.stop_listen("no speech")
        elif elapsed > self.cfg["max_utterance_s"]:
            self.finish_listen("max length")

    def close_sess(self, L):
        try:
            L["sess"].close()
        except Exception:
            pass

    def stop_listen(self, reason):
        """End listening without sending anything."""
        L, self.listen = self.listen, None
        self.gen += 1
        if L:
            self.close_sess(L)
            self.log("listen_end", mode=L["mode"], reason=reason, secs=round(L["n"] * CHUNK_S, 2), spoke=L["speech"] >= 6, sent=False)
        self.set_state("ready")

    def finish_listen(self, reason):
        L, self.listen = self.listen, None
        self.gen += 1
        self.close_sess(L)
        spoke = L["speech"] >= 6
        secs = L["n"] * CHUNK_S
        self.log("listen_end", mode=L["mode"], reason=reason, secs=round(secs, 2), spoke=spoke, sent=spoke and secs >= PTT_MIN_S)
        if not spoke or secs < PTT_MIN_S:
            self.note("Nothing heard")
            self.play("empty")
            self.set_state("ready")
            return
        self.set_state("transcribing")
        threading.Thread(target=self.stt_thread, args=(bytes(L["buf"]), L["mode"]), daemon=True).start()

    def stt_thread(self, pcm, mode):
        try:
            text = self.stt(pcm)
            self.q.put(("stt", text, mode, None))
        except Exception as e:
            self.q.put(("stt", "", mode, f"{type(e).__name__}: {e}"))

    def play(self, name):
        if not self.a.sound:
            return
        f = Path(self.a.cz_dir) / "sounds" / f"{name}.wav"
        if f.exists():
            try:
                subprocess.Popen(["pw-play", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # ---- event handling (main thread)
    def handle(self, ev):
        k = ev[0]
        if k == "key":
            self.on_key(ev[1], ev[2])
        elif k == "audio":
            self.on_audio(ev[1], ev[2])
        elif k == "sio":
            self.on_sio(ev[1], ev[2])
        elif k == "stt":
            self.on_stt(*ev[1:])
        elif k == "problem":
            self.problem = (ev[1], ev[2])
            self.log("problem", title=ev[1], lines=ev[2])
            if self.turn:
                self.end_turn("connection problem")
            if self.listen:
                self.stop_listen("connection problem")
            self.set_state(ev[3])
            self.dirty = True
        elif k == "online":
            self.problem = None
            self.set_state("ready")
        elif k == "lost":
            if self.turn:
                self.note("Connection lost")
                self.end_turn("connection lost")
            if self.listen:
                self.stop_listen("connection lost")
            self.set_state("connecting")
        elif k == "metrics":
            self.metrics, self.dirty = ev[1], True
        elif k == "sent":
            self.log("sent_ok", text=ev[1])
        elif k == "send_failed":
            self.log("send_failed", text=ev[1], error=ev[2])
            self.add_line("warn", "Not sent: " + ev[2])
            if self.turn:
                self.end_turn("send failed")

    def on_stt(self, text, mode, err):
        self.log("stt", text=text, error=err, mode=mode)
        if err:
            self.note("Speech server error")
            self.add_line("warn", "Speech-to-text failed: " + err[:60])
            self.play("error")
            self.set_state("ready" if self.connected else "offline")
        elif not text:
            self.note("Didn't catch that")
            self.play("empty")
            self.set_state("ready")
        elif self.turn or not self.connected:
            self.note("Busy - not sent")
            self.set_state("ready" if self.connected else "offline")
        else:
            self.play("ok")
            self.set_state("ready")
            self.send(text, "voice")

    def on_sio(self, name, data):
        data = data if isinstance(data, dict) else {}
        extra = {"text": data.get("text") or data.get("message")} if name in ("bot_token", "bot_message") else (
            {"talking": data.get("talking")} if name == "talking_state" else {})
        self.log(name, **extra)
        if name == "bot_stream_start":
            if not self.turn:
                self.turn = {"ours": False, "t": time.monotonic(), "phase": "thinking"}   # someone else is talking to TARS
                self.set_state("thinking")
            self.cur_tars = None
        elif name == "bot_token":
            self.mark_speaking()
            if self.cur_tars is None:
                self.cur_tars = self.add_line("tars", "")
            self.cur_tars["text"] += clean_text(data.get("text", ""))
            self.dirty = True
        elif name == "talking_state":
            if data.get("talking") and self.turn:
                self.mark_speaking()
        elif name == "bot_message":
            self.mark_speaking()
            text = clean_text(data.get("message", ""))
            who = "warn" if text.startswith("Error processing message:") else "tars"
            if self.cur_tars is not None:
                self.cur_tars.update(who=who, text=text)       # replaces the text streamed so far
            else:
                self.add_line(who, text)                        # e.g. a second message after a tool ran
            self.cur_tars = None
            self.dirty = True
        elif name == "bot_audio_done":
            was = self.turn
            self.end_turn("bot_audio_done")
            if was and was["ours"] and self.cfg["follow_up"] and self.connected and not self.listen:
                self.start_listen("followup")

    def mark_speaking(self):
        if self.turn and self.turn["phase"] != "speaking":
            self.turn["phase"] = "speaking"
            self.set_state("speaking")

    def on_key(self, code, val):
        if code in K_SHIFTS:
            self.shift = val != 0
            return
        if val == 0:
            if self.listen and self.listen["mode"] == "ptt" and code == self.talk_code:
                self.finish_listen("key released")
            return
        if val == 2:                                         # key repeat: only editing and scrolling
            if not self.listen:
                if code == K_BS:
                    self.input = self.input[:-1]
                elif code == K_UP:
                    self.scroll += 1
                elif code == K_DOWN:
                    self.scroll = max(0, self.scroll - 1)
                self.dirty = True
            return
        if self.listen:
            if self.listen["mode"] == "ptt":
                return                                       # talk key held: nothing else is accepted
            self.log("key_cancel", key=KEYNAME.get(code, code))
            self.stop_listen("key")
            if code in (self.talk_code, K_ESC, K_ENTER):
                return                                       # these only cancel; other keys also act normally
        if code == self.talk_code:
            if not self.connected:
                self.note("TARS is not connected")
            elif self.turn or self.state == "transcribing":
                self.note("Wait for TARS to finish")
            else:
                self.start_listen("ptt")
        elif code == K_ESC:
            self.input = ""
            self.dirty = True
        elif code == K_ENTER:
            if not self.input.strip():
                return
            if not self.can_send():
                why = "turn in flight" if (self.turn or self.state == "transcribing") else "not connected"
                self.note("Wait for TARS to finish" if "turn" in why else "TARS is not connected")
                self.log("send_blocked", reason=why)
                return
            text, self.input = self.input, ""
            self.send(text, "typed")
        elif code == K_BS:
            self.input = self.input[:-1]
            self.dirty = True
        elif code == K_UP:
            self.scroll += 1
            self.dirty = True
        elif code == K_DOWN:
            self.scroll = max(0, self.scroll - 1)
            self.dirty = True
        elif code in CHAR and len(self.input) < 400:
            self.input += CHAR[code][1 if self.shift else 0]
            self.dirty = True

    def check_timers(self):
        now = time.monotonic()
        if self.turn and now - self.turn["t"] > self.cfg["turn_timeout_s"]:
            self.log("turn_timeout")
            self.end_turn("timeout")
            self.note("No reply from TARS - ready again", 5)
        if self.notice and now > self.notice_until:
            self.notice = ""
            self.dirty = True
        if self.listen and self.listen["n"] == 0 and now - self.listen["t_open"] > 10:
            self.stop_listen("no audio")                        # the recorder produced nothing at all
            self.note("No audio from the mic")

    # ---- drawing
    def render(self):
        return draw_frame(self)

    def main(self):
        self.pause_file = Path(self.a.runtime_dir) / "cz-voice.pause"
        self.log("start", pid=os.getpid(), url=self.base, talk_key=self.talk_name)
        self.write_flag()
        threading.Thread(target=self.net_thread, daemon=True).start()
        if self.keysrc_factory:
            self.keysrc_factory(self.q).start()
        deadline = time.monotonic() + self.a.exit_after if self.a.exit_after else None
        while not self.stop.is_set():
            try:
                ev = self.q.get(timeout=0.05)
                while True:
                    self.handle(ev)
                    ev = self.q.get_nowait()
            except queue.Empty:
                pass
            self.check_timers()
            if deadline and time.monotonic() > deadline:
                self.exit_reason = "exit-after"
                break
            now = time.monotonic()
            if self.dirty and now - self.last_draw >= 0.08:
                self.dirty, self.last_draw = False, now
                self.display.show(self.render())

    def shutdown(self):
        self.stop.set()
        if self.listen:
            self.close_sess(self.listen)
        try:
            if self.sio and self.sio.connected:
                self.sio.disconnect()
        except Exception:
            pass
        self.remove_flag()
        self.log("exit", reason=self.exit_reason, lines=[(l["who"], l["text"]) for l in self.lines[-20:]],
                 final_state=self.state)
        try:
            self.display.show(self.render())
            self.display.close()
        except Exception:
            pass


# ------------------------------------------------------------------ metrics

def parse_metrics(js):
    """Defensive: field names are not documented. Returns a short string like '82% 48C' or ''."""
    flat = {}

    def walk(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, prefix + str(k).lower() + ".")
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            flat[prefix[:-1]] = float(o)
    try:
        walk(js)
    except Exception:
        return ""
    batt = next((v for k, v in flat.items() if "batt" in k and any(s in k for s in ("percent", "pct", "level", "charge", "capacity"))), None)
    if batt is None:
        batt = next((v for k, v in flat.items() if k.split(".")[-1] in ("battery", "batt") and 0 <= v <= 100), None)
    temp = next((v for k, v in flat.items() if "temp" in k and "cpu" in k), None)
    if temp is None:
        temp = next((v for k, v in flat.items() if "temp" in k), None)
    parts = []
    if batt is not None and 0 <= batt <= 100:
        parts.append(f"{batt:.0f}%")
    if temp is not None and 0 < temp < 130:
        parts.append(f"{temp:.0f}C")
    return " ".join(parts)


# ------------------------------------------------------------------ drawing

_FONT_CACHE = {}


def load_fonts():
    from PIL import ImageFont
    sans = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf"]
    bold = [p.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") for p in sans]
    mono = [p.replace("DejaVuSans.ttf", "DejaVuSansMono.ttf") for p in sans]

    def get(paths, size):
        for p in paths:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
        return ImageFont.load_default(size)               # Pillow's built-in scalable font
    return {"body": get(sans, 12), "bold": get(bold, 12), "small": get(sans, 10), "pill": get(bold, 11),
            "title": get(bold, 13), "mono": get(mono, 12), "big": get(bold, 14)}


def wrap(font, text, width, first_indent=0):
    """Word-wrap text to pixel width; the first line is shorter by first_indent. Long words are split."""
    out, cur, avail = [], "", width - first_indent
    for para in text.split("\n"):
        for word in para.split(" "):
            trial = (cur + " " + word) if cur else word
            if font.getlength(trial) <= avail:
                cur = trial
                continue
            if cur:
                out.append(cur)
                cur, avail = "", width
            while font.getlength(word) > avail:
                n = len(word)
                while n > 1 and font.getlength(word[:n]) > avail:
                    n -= 1
                out.append(word[:n])
                word, avail = word[n:], width
            cur = word
        out.append(cur)
        cur, avail = "", width
    return out or [""]


def conv_lines(app, width):
    """Wrapped display lines: (colour, prefix or '', text, indent). Continuation lines hang under the text."""
    f, out = app.fonts["body"], []
    for m in app.lines:
        col, prefix = {"you": (C_YOU, "You:"), "tars": (C_TARS, "TARS:"), "warn": (C_WARN, "!"), "sys": (DIM, "")}[m["who"]]
        pw = app.fonts["bold"].getlength(prefix + " ") if prefix else 0
        for i, t in enumerate(wrap(f, m["text"], width - pw)):
            out.append((col, prefix if i == 0 else "", t, pw))
    return out


_BG = {}


def background():
    """Optional picture behind the conversation: tars_bg.png next to this file. Dimmed once so text stays readable.
    Any image works; it is scaled to the screen. Missing or unreadable file = flat colour."""
    if "im" not in _BG:
        im = None
        try:
            from PIL import Image
            p = Path(__file__).resolve().parent / "tars_bg.png"
            if p.exists():
                im = Image.blend(Image.open(p).convert("RGB").resize((W, H)), Image.new("RGB", (W, H), (4, 6, 12)), 0.45)
        except Exception:
            im = None
        _BG["im"] = im
    return _BG["im"]


def draw_frame(app):
    from PIL import Image, ImageDraw
    bg = background()
    im = bg.copy() if bg else Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    F = app.fonts
    # status bar
    d.rectangle((0, 0, W, 19), fill=BAR_BG)
    d.text((6, 10), "TARS", font=F["title"], fill=(255, 190, 70), anchor="lm")
    label, col = STATES[app.state]
    if app.state == "listening" and app.listen:
        label = "Listening"
    tw = F["pill"].getlength(label)
    d.rounded_rectangle((W - tw - 17, 2, W - 3, 17), radius=7, fill=col)
    d.text((W - 3 - 7 - tw / 2, 10), label, font=F["pill"], fill=(10, 12, 16), anchor="mm")
    if app.metrics:
        d.text((W - tw - 22, 10), app.metrics, font=F["small"], fill=DIM, anchor="rm")
    # conversation area
    top, bot = 22, 146
    lh = 15
    if app.problem:
        title, rows = app.problem
        d.text((W // 2, 40), title, font=F["big"], fill=C_WARN, anchor="mm")
        y = 64
        for r in rows:
            d.text((W // 2, y), r, font=F["body"], fill=FG, anchor="mm")
            y += lh + 1
        draw_bottom(app, d, F)
        return im
    lines = conv_lines(app, W - 14)
    total = len(lines)
    if app.scroll and total > app.last_total:
        app.scroll += total - app.last_total
    app.last_total = total
    rows = (bot - top) // lh
    app.scroll = max(0, min(app.scroll, max(0, total - rows)))
    end = total - app.scroll
    vis = lines[max(0, end - rows):end]
    y = bot - len(vis) * lh
    for colr, prefix, text, pw in vis:
        if bg:                                             # 1 px shadow keeps text crisp over the picture
            if prefix:
                d.text((6, y + 1), prefix, font=F["bold"], fill=(0, 0, 0))
            d.text((6 + pw, y + 1), text, font=F["body"], fill=(0, 0, 0))
        if prefix:
            d.text((5, y), prefix, font=F["bold"], fill=colr)
        d.text((5 + pw, y), text, font=F["body"], fill=C_WARN if colr == C_WARN else FG)
        y += lh
    if not lines and app.state == "ready":
        d.text((W // 2, 80), "Hold %s to talk" % app.talk_name, font=F["body"], fill=DIM, anchor="mm")
    if total > rows:                                       # slim scroll bar
        th = max(8, (bot - top) * rows // total)
        ty = top + (bot - top - th) * (total - rows - app.scroll) // max(1, total - rows)
        d.rectangle((W - 3, top, W - 2, bot), fill=BAR_BG)
        d.rectangle((W - 3, ty, W - 2, ty + th), fill=DIM)
    draw_bottom(app, d, F)
    return im


def draw_bottom(app, d, F):
    d.rectangle((0, 148, W, H), fill=(10, 12, 18))         # solid strip so the input line never sits on the picture
    d.line((0, 148, W, 148), fill=BAR_BG)
    y = 159
    now = time.monotonic()
    if app.listen:
        L = app.listen
        if L["mode"] == "ptt":
            msg = "Release %s to send" % app.talk_name
        else:
            left = max(0, app.cfg["follow_up_wait_s"] - L["n"] * CHUNK_S) if L["speech"] < 6 else None
            msg = ("Your turn - speak (%ds)" % math.ceil(left)) if left is not None else "Listening..."
        d.text((5, y), msg, font=F["body"], fill=(255, 176, 32), anchor="lm")
        x0, x1 = 196, W - 6
        d.rectangle((x0, y - 5, x1, y + 5), outline=DIM)
        fl, thr = L.get("fl", 50.0), L.get("thr", 110.0)
        scale = lambda r: min(1.0, math.log10((r + 1) / (fl + 1)) / math.log10(60)) if r > fl else 0.0
        v = scale(app.level)
        loud = app.level > thr
        d.rectangle((x0 + 1, y - 4, x0 + 1 + int(v * (x1 - x0 - 2)), y + 4), fill=(70, 210, 110) if loud else (150, 160, 175))
        tx = x0 + 1 + int(scale(thr) * (x1 - x0 - 2))
        d.line((tx, y - 6, tx, y + 6), fill=(255, 176, 32))
        return
    if app.notice and now < app.notice_until:
        d.text((5, y), app.notice, font=F["body"], fill=C_WARN, anchor="lm")
        return
    if app.input:
        tail = app.input
        while F["mono"].getlength("> \u2026" + tail + "_") > W - 10 and len(tail) > 1:
            tail = tail[1:]
        d.text((5, y), "> " + ("\u2026" if tail != app.input else "") + tail + "_", font=F["mono"], fill=FG, anchor="lm")
        return
    hint = {"connecting": "Connecting to TARS...", "offline": "Waiting for TARS...",
            "ready": "Hold %s to talk  -  type + Enter" % app.talk_name,
            "transcribing": "Transcribing...",
            "thinking": "TARS is thinking...", "speaking": "TARS is speaking..."}.get(app.state, "")
    d.text((5, y), hint, font=F["body"], fill=DIM, anchor="lm")


# ------------------------------------------------------------------ main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="TARS remote for the CardputerZero")
    cz = os.environ.get("CZ_AI_DIR") or str(Path.home() / "cz-ai")
    p.add_argument("--cz-dir", default=cz, help="folder holding config.json and sounds/ (default ~/cz-ai)")
    p.add_argument("--config", help="config file (default <cz-dir>/config.json)")
    p.add_argument("--url"), p.add_argument("--password-file")
    p.add_argument("--runtime-dir", default=os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    p.add_argument("--exit-after", type=float, default=0, metavar="SECONDS")
    p.add_argument("--png-dir", help="write numbered PNG frames here instead of using the framebuffer")
    p.add_argument("--png-scale", type=int, default=1)
    p.add_argument("--fb", default=FB)
    p.add_argument("--script", help="timed key script instead of the keyboard device")
    p.add_argument("--kbd", default=KBD)
    p.add_argument("--wav", action="append", help="16 kHz mono s16 WAV played as the mic; repeat for one file per recording")
    p.add_argument("--wav-speed", type=float, default=1.0)
    p.add_argument("--stt-fixed", action="append", metavar="TEXT",
                   help="skip the speech server and return TEXT (repeat: one text per recording, the last repeats)")
    p.add_argument("--no-sound", dest="sound", action="store_false")
    p.add_argument("--event-log", help="append JSONL events here")
    p.add_argument("--retry-s", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    a.event_log_lock = threading.Lock()
    return a


def build(a):
    cfg = load_config(a.config or Path(a.cz_dir) / "config.json", {"url": a.url, "password_file": a.password_file})
    display = PngDisplay(a.png_dir, a.png_scale) if a.png_dir else FbDisplay(a.fb)
    mic = WavMic(a.wav, a.wav_speed) if a.wav else PwMic()
    if a.stt_fixed is not None:
        texts = list(a.stt_fixed)
        stt = lambda pcm: texts.pop(0) if len(texts) > 1 else texts[0]
    else:
        stt = lambda pcm: server_transcribe(pcm, cfg["stt_url"], cfg["stt_model"])
    if a.script:
        keys = lambda q: ScriptKeys(q, a.script)
    else:
        keys = lambda q: EvdevKeys(q, a.kbd)
    return Tars(cfg, a, display, mic, stt, keys)


def main(argv=None):
    a = parse_args(argv)
    if not os.environ.get("XDG_RUNTIME_DIR"):
        os.environ["XDG_RUNTIME_DIR"] = a.runtime_dir
    app = build(a)
    code = 0

    def on_signal(sig, frm):
        app.exit_reason = signal.Signals(sig).name
        app.stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        app.main()
    except BaseException as e:                              # the flag must go on every exit path
        app.exit_reason = f"exception {type(e).__name__}: {e}"
        code = 1
        import traceback
        traceback.print_exc()
    finally:
        app.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)                                          # do not wait for socket.io / capture threads


if __name__ == "__main__":
    main()
