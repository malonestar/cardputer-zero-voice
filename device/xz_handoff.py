#!/usr/bin/env python3
"""Hand one recorded utterance to a xiaozhi-esp32-server and play the reply.

Usage:
    xz_handoff.py <utterance.wav> [transcript text ...]
    xz_handoff.py --dry-run [utterance.wav]
    xz_handoff.py --selftest

Protocol is copied from the frAInk app (core/xiaozhi_client.py, core/audio.py,
apps/voice_base.py); see NOTES.md for the line-by-line mapping.

stdout: one JSON object per line, {"t": seconds, "event": ..., ...}.
Exit codes: 0 reply completed (server sent tts stop), 1 server/protocol error
or connection closed early, 2 timeout before tts stop, 3 connect/handshake
failure, 4 bad input or config, 5 playback (pw-cat) failure, 6 missing
dependency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import time
import uuid
import wave
import warnings
from pathlib import Path

CONFIG_PATH = Path("~/cz-ai/config.json").expanduser()

# --- constants mirrored from the existing app -------------------------------
HELLO_TIMEOUT = 10            # core/xiaozhi_client.py:12
SAMPLE_RATE_IN = 16000        # core/xiaozhi_client.py:31, core/audio.py:23
FRAME_MS = 60                 # core/xiaozhi_client.py:33, core/audio.py:25
DEFAULT_SERVER_RATE = 24000   # core/xiaozhi_client.py:85,210 / core/audio.py:24
FRAME_SAMPLES_IN = SAMPLE_RATE_IN * FRAME_MS // 1000   # 960
MAX_DECODE_MS = 120           # opus max packet duration; decode buffer only

# Defaults. Host/port are the device-side address Kyle gave; path, device id,
# protocol version are from config.example.yaml:7-11.
DEFAULTS = {
    "url": "ws://YOUR-SPEECH-SERVER:8000/api/ws",
    "device_id": "cz-01",
    "client_id": "",              # empty -> random uuid4 per run (as the app does, minus persisting)
    "protocol_version": 1,
    "listen_mode": "manual",      # push-to-talk
    "timeout": 25.0,              # seconds, measured from listen stop
    "hello_timeout": HELLO_TIMEOUT,
    "ping_interval": 20,          # core/xiaozhi_client.py:165
    "ping_timeout": 10,           # core/xiaozhi_client.py:166
    "target_peak": 0.5,
    "pace_realtime": True,        # send frames at 60 ms cadence like the live mic does
    "stop_grace": 1.0,            # wait after tts stop for another tts start (multi-sentence)
    "pw_cat": "pw-cat",
}

_T0 = time.monotonic()


def emit(event: str, **kw) -> None:
    rec = {"t": round(time.monotonic() - _T0, 3), "event": event}
    rec.update(kw)
    print(json.dumps(rec, ensure_ascii=False), flush=True)


class Fail(Exception):
    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# --- protocol builders (verbatim from core/xiaozhi_client.py) ---------------

def build_hello_message() -> dict:
    return {
        "type": "hello",
        "version": 1,
        "features": {"mcp": True},
        "transport": "websocket",
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    }


def build_listen_message(session_id: str, state: str,
                         mode: str = "auto", text: str = "") -> dict:
    msg = {
        "session_id": session_id,
        "type": "listen",
        "state": state,
    }
    if state == "start":
        msg["mode"] = mode
    if text:
        msg["text"] = text
    return msg


# --- config ------------------------------------------------------------------

def load_config(path: Path) -> tuple[dict, bool]:
    cfg = dict(DEFAULTS)
    found = path.exists()
    if found:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Fail(4, f"cannot read config {path}: {e}")
        section = data.get("xiaozhi", {}) if isinstance(data, dict) else {}
        if not isinstance(section, dict):
            raise Fail(4, f'"xiaozhi" in {path} must be an object')
        for k, v in section.items():
            if k in DEFAULTS:
                cfg[k] = v
    try:
        for k in ("timeout", "hello_timeout", "ping_interval", "ping_timeout",
                  "target_peak", "stop_grace"):
            cfg[k] = float(cfg[k])
        cfg["protocol_version"] = int(cfg["protocol_version"])
    except (TypeError, ValueError) as e:
        raise Fail(4, f"bad numeric value in config: {e}")
    if not 0.0 < cfg["target_peak"] <= 1.0:
        raise Fail(4, "target_peak must be in (0, 1]")
    if cfg["listen_mode"] not in ("manual", "auto", "realtime"):
        raise Fail(4, "listen_mode must be manual, auto or realtime")
    return cfg, found


def build_headers(cfg: dict, client_id: str) -> dict:
    # core/xiaozhi_client.py:157-161
    return {
        "Device-Id": str(cfg["device_id"]),
        "Client-Id": client_id,
        "Protocol-Version": str(cfg["protocol_version"]),
    }


# --- dependencies -------------------------------------------------------------

def import_numpy():
    try:
        import numpy as np
    except ImportError as e:
        raise Fail(6, f"numpy not installed: {e}")
    return np


def import_opus():
    # opuslib 3.0.1 emits a SyntaxWarning at first compile on Python 3.13.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            import opuslib
        except (ImportError, OSError) as e:   # same pair as core/audio.py:14
            raise Fail(6, f"opuslib/libopus not available: {e}")
    return opuslib


def import_websockets():
    try:
        import websockets
    except ImportError as e:
        raise Fail(6, f"websockets not installed: {e}")
    return websockets


# --- audio in ------------------------------------------------------------------

def load_wav_float(path: str):
    """Return (float32 mono samples in int16 units, notes)."""
    np = import_numpy()
    notes = []
    try:
        with wave.open(path, "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (OSError, EOFError, wave.Error) as e:
        raise Fail(4, f"cannot read wav {path}: {type(e).__name__}: {e}")
    if width != 2:
        raise Fail(4, f"wav must be 16-bit PCM (got {width * 8}-bit)")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
        notes.append(f"downmixed {ch} channels to mono")
    if rate != SAMPLE_RATE_IN and len(x):
        m = int(round(len(x) * SAMPLE_RATE_IN / rate))
        x = np.interp(np.linspace(0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
        notes.append(f"resampled {rate} Hz to {SAMPLE_RATE_IN} Hz (linear)")
    return x, notes


def peak_normalise(x, target_peak: float):
    """Scale so max |sample| == target_peak * 32767. Returns (int16 array, peak_before, gain)."""
    np = import_numpy()
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak < 1.0:
        raise Fail(4, "wav is silent (all samples zero)")
    gain = target_peak * 32767.0 / peak
    y = np.clip(np.round(x * gain), -32768, 32767).astype(np.int16)
    return y, peak / 32767.0, gain


def pcm_to_frames(pcm_i16) -> list[bytes]:
    """Split into 60 ms (960-sample) frames, zero-padding the last one."""
    np = import_numpy()
    n = len(pcm_i16)
    total = -(-n // FRAME_SAMPLES_IN) * FRAME_SAMPLES_IN
    if total != n:
        pcm_i16 = np.concatenate([pcm_i16, np.zeros(total - n, dtype=np.int16)])
    return [pcm_i16[i:i + FRAME_SAMPLES_IN].tobytes() for i in range(0, total, FRAME_SAMPLES_IN)]


def encode_frames(frames: list[bytes]) -> list[bytes]:
    opuslib = import_opus()
    # core/audio.py:78 -- Encoder(rate_in, 1, APPLICATION_VOIP), encode(pcm, frame_size_in)
    enc = opuslib.Encoder(SAMPLE_RATE_IN, 1, opuslib.APPLICATION_VOIP)
    return [enc.encode(f, FRAME_SAMPLES_IN) for f in frames]


def prepare_opus(path: str, target_peak: float):
    x, notes = load_wav_float(path)
    pcm, peak_before, gain = peak_normalise(x, target_peak)
    opus = encode_frames(pcm_to_frames(pcm))
    return opus, {
        "seconds": round(len(x) / SAMPLE_RATE_IN, 3),
        "peak_before": round(peak_before, 4),
        "gain": round(gain, 2),
        "peak_after": round(target_peak, 3),
        "frames": len(opus),
        "frame_ms": FRAME_MS,
        "notes": notes,
    }


# --- audio out -----------------------------------------------------------------

class Player:
    """pw-cat --playback --raw ... - subprocess fed through a queue."""

    def __init__(self, pw_cat: str):
        self.pw_cat = pw_cat
        self.proc = None
        self.rate = 0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.writer: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail = b""
        self.bytes_queued = 0
        self.error: str | None = None

    def argv(self, rate: int) -> list[str]:
        return [self.pw_cat, "--playback", "--raw", "--rate", str(rate),
                "--channels", "1", "--format", "s16", "-"]

    async def ensure_open(self, rate: int) -> bool:
        if self.proc is not None:
            return self.error is None
        self.rate = rate
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv(rate),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self.error = f"cannot start {self.pw_cat}: {e}"
            return False
        self.stderr_task = asyncio.create_task(self._read_stderr())
        self.writer = asyncio.create_task(self._write_loop())
        return True

    async def _read_stderr(self):
        while True:
            chunk = await self.proc.stderr.read(1024)
            if not chunk:
                return
            self.stderr_tail = (self.stderr_tail + chunk)[-2048:]

    async def _write_loop(self):
        stdin = self.proc.stdin
        while True:
            pcm = await self.queue.get()
            if pcm is None:
                break
            if self.error:
                continue   # keep draining so feed() never backs up
            try:
                stdin.write(pcm)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                self.error = f"{self.pw_cat} closed its input: {e!r}"
        try:
            stdin.close()
            await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def feed(self, pcm: bytes):
        if self.proc is None or self.error:
            return
        self.bytes_queued += len(pcm)
        self.queue.put_nowait(pcm)

    async def finish(self):
        """Flush queued audio, close stdin, wait for pw-cat to finish playing."""
        if self.proc is None:
            return
        self.queue.put_nowait(None)
        await self.writer
        seconds = self.bytes_queued / (2 * max(self.rate, 1))
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=seconds + 5)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
            self.error = self.error or f"{self.pw_cat} did not exit after playing {seconds:.1f}s of audio"
        await self.stderr_task
        if self.proc.returncode not in (0, None) and not self.error:
            tail = self.stderr_tail.decode("utf-8", "replace").strip()
            self.error = f"{self.pw_cat} exited {self.proc.returncode}: {tail}"
        elif self.error and self.stderr_tail:
            self.error += f" ({self.stderr_tail.decode('utf-8', 'replace').strip()})"


# --- session -------------------------------------------------------------------

class Session:
    """Server-message handling, after apps/voice_base.py:68-96."""

    def __init__(self, cfg: dict, player: Player):
        self.cfg = cfg
        self.player = player
        self.opuslib = import_opus()
        self.session_id: str | None = None
        self.server_rate = DEFAULT_SERVER_RATE
        self.playing = False          # mirrors AudioService.is_playing
        self.decoder = None
        self.stop_seen = False
        self.stt_texts: list[str] = []
        self.frames_played = 0
        self.frames_dropped = 0
        self.decode_errors = 0
        self.error: str | None = None
        self.done = asyncio.Event()
        self._grace: asyncio.TimerHandle | None = None

    def _cancel_grace(self):
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None

    async def on_json(self, msg: dict):
        t = msg.get("type")
        if t == "stt":
            # voice_base.py:72-80: mid-reply stt must not interrupt playback.
            # We never stop anything on stt; just report which kind it was.
            text = msg.get("text", "")
            self.stt_texts.append(text)
            emit("stt", text=text, mid_reply=self.playing)
        elif t == "llm":
            emit("llm", text=msg.get("text", ""), emotion=msg.get("emotion"))
        elif t == "tts":
            state = msg.get("state")
            if state == "start":
                self._cancel_grace()          # multi-sentence: a new start cancels the finish timer
                self.stop_seen = False
                # core/audio.py:144 start_playback: fresh Decoder(rate_out, 1)
                self.decoder = self.opuslib.Decoder(self.server_rate, 1)
                self.playing = True
                if not await self.player.ensure_open(self.server_rate):
                    emit("error", where="playback", message=self.player.error)
                emit("tts", state="start", rate=self.server_rate)
            elif state == "sentence_start":
                emit("tts", state="sentence_start", text=msg.get("text", ""))
            elif state == "stop":
                self.playing = False          # core/audio.py:176 stop_playback
                self.decoder = None
                self.stop_seen = True
                emit("tts", state="stop", frames_played=self.frames_played)
                self._cancel_grace()
                loop = asyncio.get_running_loop()
                self._grace = loop.call_later(self.cfg["stop_grace"], self.done.set)
            else:
                emit("ignored", type=t, state=state)
        else:
            # hello (late), mcp, etc.: the app ignores them too.
            emit("ignored", type=t)

    def on_audio(self, data: bytes):
        # voice_base.py:92-96: play whatever arrives while playback is open.
        if not self.playing or self.decoder is None:
            self.frames_dropped += 1
            return
        max_frame = self.server_rate * MAX_DECODE_MS // 1000
        try:
            pcm = self.decoder.decode(data, max_frame)
        except self.opuslib.OpusError as e:
            self.decode_errors += 1
            if self.decode_errors == 1:
                emit("error", where="opus_decode", message=str(e))
            return
        self.frames_played += 1
        self.player.feed(pcm)

    def on_closed(self, reason: str):
        if not self.stop_seen and self.error is None:
            self.error = f"connection closed before tts stop ({reason})"
        self.done.set()


async def recv_loop(ws, sess: Session, websockets):
    try:
        async for message in ws:
            if isinstance(message, bytes):
                sess.on_audio(message)
                continue
            try:
                msg = json.loads(message)
            except ValueError:
                emit("error", where="server_json", message="unparseable text frame")
                continue
            if isinstance(msg, dict):
                await sess.on_json(msg)
        sess.on_closed("closed")
    except websockets.ConnectionClosed as e:
        sess.on_closed(f"{e.rcvd.code if e.rcvd else 'no close frame'}")


async def send_utterance(ws, sess: Session, opus: list[bytes], cfg: dict):
    await ws.send(json.dumps(build_listen_message(sess.session_id, "start", mode=cfg["listen_mode"])))
    t_start = time.monotonic()
    for i, frame in enumerate(opus):
        await ws.send(frame)
        if cfg["pace_realtime"]:
            delay = t_start + (i + 1) * FRAME_MS / 1000 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
    await ws.send(json.dumps(build_listen_message(sess.session_id, "stop")))


async def run(cfg: dict, wav: str, transcript: str) -> int:
    websockets = import_websockets()
    if shutil.which(cfg["pw_cat"]) is None:
        raise Fail(5, f"{cfg['pw_cat']} not found in PATH")
    opus, stats = prepare_opus(wav, cfg["target_peak"])
    emit("audio_prepared", **stats, **({"local_transcript": transcript} if transcript else {}))

    client_id = cfg["client_id"] or str(uuid.uuid4())
    headers = build_headers(cfg, client_id)
    player = Player(cfg["pw_cat"])
    sess = Session(cfg, player)
    timed_out = False

    try:
        async with websockets.connect(
            cfg["url"], additional_headers=headers,
            ping_interval=cfg["ping_interval"], ping_timeout=cfg["ping_timeout"],
            open_timeout=cfg["hello_timeout"],
        ) as ws:
            # core/xiaozhi_client.py:200-212 handshake
            await ws.send(json.dumps(build_hello_message()))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=cfg["hello_timeout"])
            except TimeoutError:
                raise Fail(3, "server hello timeout")
            if isinstance(raw, bytes):
                raise Fail(3, "expected JSON hello, got binary frame")
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise Fail(3, f"expected hello, got {hello.get('type')}")
            sess.session_id = hello.get("session_id")
            sess.server_rate = (hello.get("audio_params") or {}).get("sample_rate", DEFAULT_SERVER_RATE)
            emit("connected", url=cfg["url"], session_id=sess.session_id,
                 server_sample_rate=sess.server_rate, client_id=client_id,
                 device_id=headers["Device-Id"])

            rx = asyncio.create_task(recv_loop(ws, sess, websockets))
            try:
                await send_utterance(ws, sess, opus, cfg)
                emit("sent", frames=len(opus), mode=cfg["listen_mode"])
                try:
                    async with asyncio.timeout(cfg["timeout"]):
                        await sess.done.wait()
                except TimeoutError:
                    timed_out = True
            finally:
                sess._cancel_grace()
                rx.cancel()
                await asyncio.gather(rx, return_exceptions=True)
    except Fail:
        raise
    except websockets.ConnectionClosed as e:
        sess.error = sess.error or f"connection closed while sending ({e})"
    except (OSError, websockets.InvalidHandshake, TimeoutError) as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        raise Fail(3, f"connect failed: {type(e).__name__}: {e}", http_status=status)
    finally:
        await player.finish()

    if player.error:
        emit("error", where="playback", message=player.error)
    if sess.frames_dropped:
        emit("warning", message=f"{sess.frames_dropped} audio frame(s) arrived outside tts start/stop and were dropped")
    if sess.error:
        emit("error", where="session", message=sess.error)
        code = 1
    elif timed_out and not sess.stop_seen:
        emit("error", where="timeout", message=f"no tts stop within {cfg['timeout']}s",
             frames_played=sess.frames_played)
        code = 2
    elif player.error:
        code = 5
    else:
        code = 0
        if sess.frames_played == 0:
            emit("warning", message="tts stop received but no audio frames were played")
    emit("done", ok=code == 0, exit_code=code, frames_played=sess.frames_played,
         frames_dropped=sess.frames_dropped, stt=sess.stt_texts[-1] if sess.stt_texts else None)
    return code


# --- dry run / selftest ---------------------------------------------------------

def dry_run(cfg: dict, cfg_found: bool, wav: str | None) -> int:
    client_id = cfg["client_id"] or "<random uuid4 generated per run>"
    sid = "<session_id from server hello>"
    info = {
        "config_path": str(CONFIG_PATH), "config_found": cfg_found,
        "url": cfg["url"],
        "headers": build_headers(cfg, client_id),
        "hello": build_hello_message(),
        "listen_start": build_listen_message(sid, "start", mode=cfg["listen_mode"]),
        "listen_stop": build_listen_message(sid, "stop"),
        "opus": {"encoder": "libopus VOIP", "rate": SAMPLE_RATE_IN, "channels": 1, "frame_ms": FRAME_MS},
        "playback_cmd": Player(cfg["pw_cat"]).argv(DEFAULT_SERVER_RATE),
        "playback_rate_note": "rate comes from the server hello audio_params.sample_rate (24000 if absent)",
        "pw_cat_found": shutil.which(cfg["pw_cat"]) is not None,
        "timeout": cfg["timeout"], "stop_grace": cfg["stop_grace"],
        "pace_realtime": cfg["pace_realtime"],
    }
    if wav:
        x, notes = load_wav_float(wav)
        _, peak_before, gain = peak_normalise(x, cfg["target_peak"])
        info["audio"] = {"seconds": round(len(x) / SAMPLE_RATE_IN, 3),
                         "peak_before": round(peak_before, 4), "gain": round(gain, 2),
                         "frames": -(-len(x) // FRAME_SAMPLES_IN), "notes": notes}
    emit("dry_run", **info)
    return 0


def _dominant_freq(np, pcm_i16, rate: int) -> float:
    x = pcm_i16.astype(np.float64)
    x *= np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x))
    return float(np.argmax(spec) * rate / len(x))


def selftest(cfg: dict) -> int:
    np = import_numpy()
    opuslib = import_opus()
    ok = True

    def check(name: str, good: bool, **kw):
        nonlocal ok
        ok = ok and good
        emit("selftest_check", name=name, ok=bool(good), **kw)

    # 1. 16 kHz uplink path: quiet 440 Hz tone (peak ~1%) -> normalise -> 60 ms Opus -> decode
    freq, secs = 440.0, 1.2
    t = np.arange(int(SAMPLE_RATE_IN * secs)) / SAMPLE_RATE_IN
    quiet = (0.01 * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    pcm, peak_before, gain = peak_normalise(quiet, cfg["target_peak"])
    check("normalise_peak", abs(int(np.max(np.abs(pcm))) / 32767 - cfg["target_peak"]) < 0.01,
          peak_before=round(peak_before, 4), gain=round(gain, 1))
    frames = pcm_to_frames(pcm)
    check("frame_count", len(frames) == math.ceil(len(pcm) / FRAME_SAMPLES_IN) and all(len(f) == FRAME_SAMPLES_IN * 2 for f in frames),
          frames=len(frames))
    packets = encode_frames(frames)
    check("encode", all(len(p) > 0 for p in packets), packets=len(packets),
          bytes=sum(len(p) for p in packets))
    dec = opuslib.Decoder(SAMPLE_RATE_IN, 1)
    out = [dec.decode(p, SAMPLE_RATE_IN * MAX_DECODE_MS // 1000) for p in packets]
    check("decode_frame_len_16k", all(len(o) == FRAME_SAMPLES_IN * 2 for o in out),
          bytes_per_frame=len(out[0]))
    dec_pcm = np.frombuffer(b"".join(out), dtype=np.int16)
    mid = dec_pcm[len(dec_pcm) // 4: 3 * len(dec_pcm) // 4]
    f_hat = _dominant_freq(np, mid, SAMPLE_RATE_IN)
    rms_in = float(np.sqrt(np.mean(pcm[len(pcm) // 4: 3 * len(pcm) // 4].astype(np.float64) ** 2)))
    rms_out = float(np.sqrt(np.mean(mid.astype(np.float64) ** 2)))
    check("roundtrip_16k_freq", abs(f_hat - freq) < 15, expected=freq, measured=round(f_hat, 1))
    check("roundtrip_16k_level", 0.7 < rms_out / rms_in < 1.3, rms_ratio=round(rms_out / rms_in, 3))

    # 2. 24 kHz downlink path (server sample rate): encode like the server would, decode like Session does
    rate = DEFAULT_SERVER_RATE
    fs = rate * FRAME_MS // 1000
    t24 = np.arange(rate) / rate
    tone24 = (0.4 * 32767 * np.sin(2 * np.pi * 880.0 * t24)).astype(np.int16)
    enc24 = opuslib.Encoder(rate, 1, opuslib.APPLICATION_AUDIO)
    pk24 = [enc24.encode(tone24[i:i + fs].tobytes(), fs) for i in range(0, len(tone24) - fs + 1, fs)]
    dec24 = opuslib.Decoder(rate, 1)
    o24 = [dec24.decode(p, rate * MAX_DECODE_MS // 1000) for p in pk24]
    check("decode_frame_len_24k", all(len(o) == fs * 2 for o in o24), bytes_per_frame=len(o24[0]))
    d24 = np.frombuffer(b"".join(o24), dtype=np.int16)
    f24 = _dominant_freq(np, d24[len(d24) // 4: 3 * len(d24) // 4], rate)
    check("roundtrip_24k_freq", abs(f24 - 880.0) < 15, expected=880.0, measured=round(f24, 1))

    emit("selftest_info", pw_cat=shutil.which(cfg["pw_cat"]), opuslib=getattr(opuslib, "__version__", "?"),
         note="codec path only; server protocol and pw-cat playback are not exercised")
    emit("selftest", ok=ok)
    return 0 if ok else 1


# --- main ------------------------------------------------------------------------

def main(argv=None) -> int:
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Send one WAV utterance to xiaozhi-esp32-server and play the reply.")
    ap.add_argument("wav", nargs="?", help="16 kHz mono 16-bit WAV")
    ap.add_argument("transcript", nargs="*", help="optional local transcript (logged only)")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="config JSON (default ~/cz-ai/config.json)")
    ap.add_argument("--timeout", type=float, help="override config timeout (seconds after listen stop)")
    ap.add_argument("--dry-run", action="store_true", help="print hello/URL/headers and exit")
    ap.add_argument("--selftest", action="store_true", help="local Opus encode/decode check, no server")
    args = ap.parse_args(argv)

    CONFIG_PATH = Path(args.config).expanduser()
    try:
        cfg, found = load_config(CONFIG_PATH)
        if args.timeout is not None:
            cfg["timeout"] = args.timeout
        if args.selftest:
            return selftest(cfg)
        if args.dry_run:
            return dry_run(cfg, found, args.wav)
        if not args.wav:
            raise Fail(4, "missing <utterance.wav>")
        transcript = " ".join(args.transcript).strip()
        return asyncio.run(run(cfg, args.wav, transcript))
    except Fail as e:
        emit("error", where="fatal", code=e.code, message=str(e), **e.extra)
        return e.code
    except KeyboardInterrupt:
        emit("error", where="fatal", code=130, message="interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
