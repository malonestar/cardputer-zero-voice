"""On-screen indicator for the voice layer.

A small pill at the top of the screen, drawn straight onto the framebuffer so it sits on top of the launcher or
whatever app is open. The pixels underneath are saved when the pill appears and put back when it goes away.
While visible it is redrawn about 8 times a second, so an app that keeps painting cannot bury it for long.
If the screen is asleep the backlight is lit dimly for as long as the pill shows, then turned off again.

    ov = Overlay()
    ov.show("listen", "Listening")          # stays until hide() or the next show()
    ov.level(2.5)                           # loudness relative to room noise, drives the meter
    ov.show("ok", "Open Camera", hold=1.4)  # goes away by itself after 1.4 s
    ov.hide()

Kinds: listen (red, with meter), work (amber, animated dots), heard (white: what was transcribed), ok (green, tick),
ask (blue), none (grey), error (red).
"""
import threading
import time
from collections import deque
from pathlib import Path

FB = "/dev/fb0"
W, H = 320, 170
X0, Y0, PW, PH = 8, 1, 304, 22                 # pill box; kept above y=24, which the tools sample to recognise the home screen
BACKLIGHT = Path("/sys/class/backlight/backlight/brightness")
SLEEP_LEVEL = 37                               # backlight used to light a sleeping screen; also how we know it was us
COLORS = {"listen": (255, 84, 84), "work": (255, 184, 48), "ok": (70, 220, 120), "ask": (90, 200, 255),
          "none": (150, 158, 170), "error": (255, 96, 96), "heard": (225, 230, 240)}
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


class Overlay:
    def __init__(self, enabled=True, fb=FB):
        self.enabled, self.fb = enabled, fb
        self.lock = threading.RLock()
        self.visible = False
        self.kind, self.text, self.until = "none", "", None
        self.levels = deque([0.0] * 7, maxlen=7)
        self.saved = None                      # RGB image of what was under the pill
        self.saved_raw = None                  # the same pixels as raw framebuffer bytes, one entry per row
        self.lit = False
        self.font = None
        self._thread = None

    # ---------------------------------------------------------------- public

    def show(self, kind, text, hold=None):
        if not self.enabled:
            return
        try:
            with self.lock:
                if not self.visible:
                    self._save()
                    self._light()
                    self.visible = True
                self.kind, self.text = kind if kind in COLORS else "none", str(text or "")
                self.until = time.monotonic() + hold if hold else None
                self._draw()
                if not (self._thread and self._thread.is_alive()):
                    self._thread = threading.Thread(target=self._run, daemon=True)
                    self._thread.start()
        except Exception:
            self.visible = False               # never let a drawing problem disturb the voice layer

    def level(self, ratio):
        self.levels.append(max(0.0, min(1.0, (float(ratio) - 1.0) / 5.0)))

    def hide(self):
        try:
            with self.lock:
                if not self.visible:
                    return
                self.visible = False
                self._restore()
                self._unlight()
        except Exception:
            pass

    # ---------------------------------------------------------------- internals

    def _run(self):
        while True:
            time.sleep(0.12)
            with self.lock:
                if not self.visible:
                    return
                if self.until and time.monotonic() >= self.until:
                    self.hide()
                    return
                try:
                    self._draw()
                except Exception:
                    return

    def _save(self):
        from PIL import Image
        rows = []
        with open(self.fb, "rb") as f:
            for y in range(Y0, Y0 + PH):
                f.seek((y * W + X0) * 2)
                rows.append(f.read(PW * 2))
        self.saved_raw = rows
        self.saved = Image.frombytes("RGB", (PW, PH), b"".join(rows), "raw", "BGR;16")

    def _write(self, rows):
        with open(self.fb, "r+b") as f:
            for i, row in enumerate(rows):
                f.seek(((Y0 + i) * W + X0) * 2)
                f.write(row)

    def _restore(self):
        if self.saved_raw:
            self._write(self.saved_raw)
        self.saved = self.saved_raw = None

    def _light(self):
        try:
            if int(BACKLIGHT.read_text().strip() or 0) == 0:
                BACKLIGHT.write_text(str(SLEEP_LEVEL))
                self.lit = True
        except Exception:
            self.lit = False

    def _unlight(self):
        if self.lit:
            try:                               # only if nobody else (the launcher waking up) changed it meanwhile
                if int(BACKLIGHT.read_text().strip() or 0) == SLEEP_LEVEL:
                    BACKLIGHT.write_text("0")
            except Exception:
                pass
        self.lit = False

    def _font(self):
        if self.font is None:
            from PIL import ImageFont
            for p in FONTS:
                try:
                    self.font = ImageFont.truetype(p, 12)
                    break
                except Exception:
                    continue
            else:
                self.font = ImageFont.load_default()
        return self.font

    def render(self):
        """The pill as an RGB image composed over the saved background."""
        from PIL import Image, ImageDraw
        col, f, t = COLORS[self.kind], self._font(), time.monotonic()
        pill = Image.new("RGBA", (PW, PH), (0, 0, 0, 0))
        d = ImageDraw.Draw(pill)
        d.rounded_rectangle((0, 0, PW - 1, PH - 1), radius=PH // 2, fill=(8, 10, 16, 240), outline=col + (255,), width=2)
        cy = PH // 2
        if self.kind == "ok":                                        # tick
            d.line((9, cy, 13, cy + 4, 20, cy - 5), fill=col, width=2)
        else:
            r = 5 if self.kind != "listen" or int(t * 2.5) % 2 == 0 else 3      # the red dot pulses while listening
            d.ellipse((14 - r, cy - r, 14 + r, cy + r), fill=col)
        right = PW - 12
        if self.kind == "listen":                                    # loudness meter
            right = PW - 62
            for i, v in enumerate(self.levels):
                h = 3 + int(v * (PH - 10))
                x = PW - 56 + i * 7
                d.rounded_rectangle((x, cy - h // 2, x + 4, cy + (h + 1) // 2), radius=2, fill=col)
        text = self.text + ("." * (1 + int(t * 3) % 3) if self.kind == "work" else "")
        room = right - 28
        if f.getlength(text) > room:
            while text and f.getlength(text + "…") > room:
                text = text[:-1]
            text = text.rstrip() + "…"
        d.text((28, cy - 1), text, font=f, fill=(240, 243, 248) if self.kind in ("listen", "work", "heard") else col, anchor="lm")
        base = (self.saved or Image.new("RGB", (PW, PH))).convert("RGBA")
        base.alpha_composite(pill)
        return base.convert("RGB")

    def _draw(self):
        import numpy as np
        a = np.asarray(self.render(), dtype=np.uint16)
        px = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
        raw = px.astype("<u2").tobytes()
        self._write([raw[i * PW * 2:(i + 1) * PW * 2] for i in range(PH)])


if __name__ == "__main__":                     # python overlay.py demo  -> runs through the states on the real screen
    import sys
    ov = Overlay()
    steps = [("listen", "Listening", 2.0), ("work", "Transcribing", 1.5), ("ok", "Open the camera", 1.5),
             ("ask", "Asking xiaozhi: what is the tallest mountain in Colorado", 1.5), ("none", "Didn't catch that", 1.2),
             ("error", "Can't reach the server", 1.2)]
    if len(sys.argv) > 2:
        steps = [(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 2.0)]
    for kind, text, secs in steps:
        ov.show(kind, text)
        end = time.monotonic() + secs
        while time.monotonic() < end:
            ov.level(1 + 5 * abs((time.monotonic() * 3) % 2 - 1))
            time.sleep(0.05)
    ov.hide()
