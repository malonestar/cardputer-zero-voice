"""Fixed-command spotting with the sherpa-onnx keyword spotter (low memory).

Runtime needs only: sherpa_onnx, numpy, the model files and keywords.txt.
sentencepiece is needed ONLY by build_keywords() (run it off-device).

Model: sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 (BPE units).
"""
import os
import sys
import time

import numpy as np

SAMPLE_RATE = 16000

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
_FIXED = {
    "home": ["go home", "close this", "go back"],
    "volume_up": ["volume up", "turn it up", "louder"],
    "volume_down": ["volume down", "turn it down", "quieter"],
    "mute": ["mute"],
    "brightness_up": ["brightness up", "brighter"],
    "brightness_down": ["brightness down", "dimmer"],
    "battery": ["battery level", "how much battery"],
    "temperature": ["device temperature", "how hot are you"],
    "wifi": ["wifi status", "ip address"],
    "status": ["system status"],
    "time": ["what time is it"],
    "unlock": ["unlock", "unlock the screen", "wake up"],
    "sleep": ["sleep", "go to sleep", "lock the screen"],
    # the TARS remote app; "tarz" is a second spelling of the same spoken word, which the model hears more often
    "open:TARS": [p + n for n in ("tars", "tarz") for p in ("", "open ", "launch ", "open the ", "talk to ")],
    # "wifi" is spelled three ways because the model's vocabulary has no single good spelling for it
    "wifi_on": ["turn on " + w for w in ['wifi', 'wi fi', 'why fi', 'the wifi', 'the wi fi', 'the why fi', 'wireless']] + ["wireless on", "turn on the network", "network on"],
    "wifi_off": ["turn off " + w for w in ['wifi', 'wi fi', 'why fi', 'the wifi', 'the wi fi', 'the why fi', 'wireless']] + ["wireless off", "turn off the network", "network off"],
}

# app label -> spoken names
_APPS = {
    "Settings": ["settings"],
    "CLI": ["terminal", "command line"],
    "Rec": ["recorder"],
    "Math": ["calculator"],
    "Files": ["files", "file manager"],
    "Store": ["store", "app store"],
    "ZClaw": ["z claw"],
    "IP Panel": ["i p panel"],
    "SSH": ["s s h"],
    "Game": ["game"],
    "Tank": ["tank"],
    "Keyboard Guide": ["keyboard guide"],
    "GPS": ["g p s"],
    "LoRa": ["lora"],
    "NFC": ["n f c"],
    "SubG Chat": ["sub g chat"],
    "IR Remote": ["i r remote", "remote"],
    "IR Chat": ["i r chat"],
    "Camera": ["camera"],
    "Music": ["music", "music player"],
    "Piano": ["piano"],
    "Compass": ["compass"],
    "Python": ["python"],
}


def _build_commands():
    cmds = {k: list(v) for k, v in _FIXED.items()}
    for app, names in _APPS.items():
        phrases = []
        for n in names:
            phrases += ["open " + n, "launch " + n]
            if n.split()[0] != "s" and len(n.split()) <= 2 and not n.startswith(("i ", "g ", "n ", "s ")):
                phrases.append("open the " + n)
        cmds["open:" + app] = phrases
    return cmds


COMMANDS = _build_commands()


# --------------------------------------------------------------------------
# Keyword file generation (off-device; needs sentencepiece)
# --------------------------------------------------------------------------
def _wire_label(label):
    """Label as written after '@' in keywords.txt (no whitespace allowed)."""
    return label.replace(" ", "_")


def tokenize(phrases, model_dir):
    """BPE-tokenise phrases the same way sherpa_onnx.text2token(tokens_type='bpe')
    does (which would additionally import pypinyin, so we avoid it)."""
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor()
    sp.load(os.path.join(model_dir, "bpe.model"))
    return sp.encode([p.upper() for p in phrases], out_type=str)


def build_keywords(model_dir, out_path, score=1.5, threshold=0.25, commands=None):
    """Write keywords.txt: '<tokens> :<score> #<threshold> @<label>' per phrase."""
    commands = commands or COMMANDS
    lines = []
    for label, phrases in commands.items():
        for toks in tokenize(phrases, model_dir):
            lines.append("%s :%g #%g @%s" % (" ".join(toks), score, threshold, _wire_label(label)))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------
class CommandSpotter:
    """Streaming keyword spotter.

    gain: "auto" (peak tracking, default) or a fixed float multiplier.
    Auto: tracks a decaying peak of the input, scales so that peak -> target_peak
    (default 0.3 of full scale), never attenuates, capped at max_gain. A floor on
    the tracked peak stops pure silence/noise being blown up to full scale.
    """

    def __init__(self, model_dir, keywords_path, num_threads=1, gain="auto",
                 max_gain=100.0, target_peak=0.3, peak_floor=0.003,
                 keywords_score=1.5, keywords_threshold=0.25,
                 max_active_paths=4, num_trailing_blanks=1, int8=True,
                 labels=None):
        import sherpa_onnx

        suffix = ".int8.onnx" if int8 else ".onnx"
        tag = "epoch-12-avg-2-chunk-16-left-64"
        p = lambda n: os.path.join(model_dir, "%s-%s%s" % (n, tag, suffix))
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(model_dir, "tokens.txt"),
            encoder=p("encoder"), decoder=p("decoder"), joiner=p("joiner"),
            keywords_file=keywords_path,
            num_threads=num_threads,
            sample_rate=SAMPLE_RATE, feature_dim=80,
            max_active_paths=max_active_paths,
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
            num_trailing_blanks=num_trailing_blanks,
            provider="cpu",
        )
        self.gain = gain
        self.max_gain = max_gain
        self.target_peak = target_peak
        self.peak_floor = peak_floor
        self._labels = {_wire_label(l): l for l in (labels or COMMANDS)}
        self._stream = None
        self._peak = 0.0
        self.start()

    def start(self):
        self._stream = self._kws.create_stream()
        self._peak = 0.0

    def reset(self):
        """Clear decoder state after (or between) detections; keeps the stream."""
        if self._stream is None:
            self.start()
        else:
            self._kws.reset_stream(self._stream)

    def _apply_gain(self, x):
        if self.gain != "auto":
            return x * float(self.gain)
        pk = float(np.max(np.abs(x))) if x.size else 0.0
        # fast attack, slow decay (~2 s at 20 ms chunks)
        self._peak = pk if pk > self._peak else self._peak * 0.99 + pk * 0.01
        eff = max(self._peak, self.peak_floor)
        g = min(max(self.target_peak / eff, 1.0), self.max_gain)
        return x * g

    def feed(self, pcm_bytes):
        """Feed int16 mono 16 kHz PCM bytes. Returns a label (e.g. 'volume_up',
        'open:IP Panel') when a command is spotted, else None."""
        x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size == 0:
            return None
        x = np.clip(self._apply_gain(x), -1.0, 1.0)
        s = self._stream
        s.accept_waveform(SAMPLE_RATE, x)
        hit = None
        while self._kws.is_ready(s):
            self._kws.decode_stream(s)
            r = self._kws.get_result(s)
            if r:
                hit = self._labels.get(r, r)
                self._kws.reset_stream(s)
        return hit


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def _rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024.0
    return -1.0


def _read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def _selftest(model_dir, extra_gain=1.0):
    import tempfile
    # model's own test keywords, added temporarily (label = the phrase itself)
    test = {"LIGHT UP": ["light up"], "LOVELY CHILD": ["lovely child"], "FOREVER": ["forever"]}
    all_cmds = dict(COMMANDS)
    all_cmds.update(test)
    tmp = os.path.join(tempfile.mkdtemp(), "kw_test.txt")
    n = build_keywords(model_dir, tmp, commands=all_cmds)
    print("test keywords file: %d lines" % n)
    print("RSS before model: %.1f MB" % _rss_mb())
    sp = CommandSpotter(model_dir, tmp, labels=all_cmds)
    print("RSS after load:   %.1f MB" % _rss_mb())
    chunk = 320 * 2  # 20 ms
    for name in ("0.wav", "1.wav"):
        pcm = _read_wav(os.path.join(model_dir, "test_wavs", name))
        if extra_gain != 1.0:
            a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * extra_gain
            pcm = a.astype(np.int16).tobytes()
        sp.start()
        t0 = time.perf_counter()
        hits = []
        for i in range(0, len(pcm), chunk):
            h = sp.feed(pcm[i:i + chunk])
            if h:
                hits.append((round(i / 2 / SAMPLE_RATE, 2), h))
        dt = time.perf_counter() - t0
        dur = len(pcm) / 2 / SAMPLE_RATE
        print("%s (x%g level): %.1fs audio, RTF %.3f, detections: %s" % (name, extra_gain, dur, dt / dur, hits))
    print("RSS peak-ish after run: %.1f MB" % _rss_mb())


if __name__ == "__main__":
    md = sys.argv[1] if len(sys.argv) > 1 else "/tmp/kwsdl/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
    _selftest(md)
    _selftest(md, extra_gain=0.01)  # quiet audio, ~1% level
