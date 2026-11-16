#!/usr/bin/env python3
"""Wake word listener for the CardputerZero.

Same engine and models as the PiSugar suite (pymicro-wakeword / microWakeWord).
Audio comes from PipeWire (pw-cat) so the mic stays shareable with other apps.
One model listens by default (config.json can list more; features are computed once per frame).

On detection: append a JSON line to logs/wake.jsonl, play a chime, and run the
optional `on_wake` command from config.json (hook for the voice pipeline).
"""
import json
import math
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

BASE = Path(__file__).resolve().parent
MODELS_DIR = BASE / "models"
LOG = BASE / "logs" / "wake.jsonl"
CHIME = BASE / "wake_chime.wav"
CONFIG = BASE / "config.json"

DEFAULTS = {
    "models": ["okay_nabu"],     # one wake word at a time; switch with the wake_word tool
    "cutoff": None,          # None = use each model's own probability_cutoff
    "cooldown_s": 2.0,
    "chime": True,
    "on_wake": None,         # e.g. ["/home/YOUR_USER/cz-ai/venv/bin/python", "voice.py"]
}
RATE = 16000
CHUNK = 320 * 2              # 20 ms of int16 mono


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()).get("wake", {}))
        except Exception as e:
            print(f"config.json ignored: {e}", flush=True)
    return cfg


def ensure_chime():
    if CHIME.exists():
        return
    frames = bytearray()
    for freq, dur in ((880, 0.07), (1320, 0.10)):
        n = int(RATE * dur)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400)
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / RATE)))
    with wave.open(str(CHIME), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def main():
    cfg = load_config()
    ensure_chime()
    LOG.parent.mkdir(exist_ok=True)

    models = []
    for name in cfg["models"]:
        path = MODELS_DIR / f"{name}.json"
        if not path.exists():
            print(f"model missing, skipped: {name}", flush=True)
            continue
        m = MicroWakeWord.from_config(path)
        if cfg["cutoff"] is not None:
            m.probability_cutoff = cfg["cutoff"]
        models.append((name, m))
        print(f"loaded {name} (cutoff {m.probability_cutoff:.2f})", flush=True)
    if not models:
        sys.exit("no wake word models loaded")

    features = MicroWakeWordFeatures()
    rec = subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--rate", str(RATE), "--channels", "1",
         "--format", "s16", "--latency", "20ms", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print("listening", flush=True)
    last = 0.0
    try:
        while True:
            pcm = rec.stdout.read(CHUNK)
            if not pcm:
                sys.exit("audio capture ended")
            for feat in features.process_streaming(pcm):
                hit = None
                for name, m in models:
                    if m.process_streaming(feat) and hit is None:
                        hit = name
                if hit and time.monotonic() - last > cfg["cooldown_s"]:
                    last = time.monotonic()
                    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": hit}
                    print(f"WAKE {hit}", flush=True)
                    with LOG.open("a") as f:
                        f.write(json.dumps(event) + "\n")
                    if cfg["chime"]:
                        subprocess.Popen(["pw-play", str(CHIME)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if cfg["on_wake"]:
                        subprocess.Popen(list(cfg["on_wake"]) + [hit], cwd=str(BASE),
                                         env=dict(os.environ, CZ_WAKE_MODEL=hit))
                    for _, m in models:
                        m.reset()
                    features.reset()
                    break
    finally:
        rec.terminate()


if __name__ == "__main__":
    main()
