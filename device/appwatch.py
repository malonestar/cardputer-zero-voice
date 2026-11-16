#!/usr/bin/env python3
"""Frees memory for apps: stops the voice layer and the tool server while an app is open, starts them again
at the launcher.

Asks the launcher once a second what is on screen (its local control interface, `current`):
  mode external -> an app is running        -> after a short grace period, stop cz-voice and cz-mcp
  anything else  -> home screen or a launcher page -> start them again straight away

Tailscale is never touched. Settings live in ~/cz-ai/config.json under "apps":
  mode      "stop" (default) or "keep" (never stop anything)
  grace_s   how long an app must have been open before stopping (default 2; lets a voice command that
            opened the app finish cleanly)
  keep_for  app names for which nothing is stopped, e.g. ["Recorder"]
Log: ~/cz-ai/logs/appwatch.jsonl (one line per stop and start, with available memory before and after).

If the launcher stops answering for 10 s while the services are stopped, they are started again, so a launcher
problem can never leave voice switched off.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
LOG = BASE / "logs" / "appwatch.jsonl"
UNITS = ["cz-voice.service", "cz-mcp.service"]           # stop order; started in reverse (voice needs the tool server)
RPC = "tcp://127.0.0.1:5557"
DEFAULTS = {"mode": "stop", "grace_s": 2.0, "poll_s": 1.0, "keep_for": [], "unknown_s": 10.0}
MARK = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "cz-appwatch.stopped"


def settings():
    out = dict(DEFAULTS)
    try:
        out.update(json.loads(CONFIG.read_text()).get("apps") or {})
    except Exception:
        pass
    return out


def log(event, **kw):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}) + "\n")
    except Exception:
        pass


def available_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def launcher_state(endpoint=RPC, timeout_ms=1500):
    """{'mode': 'home'|'page'|'external', 'selected': ..., 'foreground': ...} or None if the launcher did not answer."""
    import zmq
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, 1000)
    try:
        sock.connect(endpoint)
        sock.send_string("current")
        body = sock.recv_multipart()[0].decode(errors="replace")
        if not body.startswith("OK"):
            return None
        return dict(l.split("=", 1) for l in body.splitlines()[1:] if "=" in l)
    except Exception:
        return None
    finally:
        sock.close()


def systemctl(action, units):
    return subprocess.run(["systemctl", "--user", action, *units], capture_output=True, text=True, timeout=60).returncode


def kept(state, keep_for):
    names = " ".join(str(state.get(k, "")) for k in ("selected", "foreground")).lower()
    return any(str(n).lower() in names for n in keep_for if str(n).strip())


class Watch:
    """The decision logic, separate from the loop so it can be tested: feed it launcher states and a clock."""

    def __init__(self, stop, start, stopped=False):
        self.stop, self.start = stop, start
        self.stopped = stopped
        self.ext_since = None
        self.unknown_since = None

    def step(self, state, cfg, now):
        if state is None:                                    # launcher not answering
            self.ext_since = None
            if self.unknown_since is None:
                self.unknown_since = now
            if self.stopped and now - self.unknown_since >= cfg["unknown_s"]:
                self._start("launcher not answering")
            return
        self.unknown_since = None
        want_stop = cfg["mode"] == "stop" and state.get("mode") == "external" and not kept(state, cfg["keep_for"])
        if not want_stop:
            self.ext_since = None
            if self.stopped:
                self._start(state.get("mode") or "launcher")
            return
        if self.ext_since is None:
            self.ext_since = now
        if not self.stopped and now - self.ext_since >= cfg["grace_s"]:
            before = available_mb()
            self.stop()
            self.stopped = True
            MARK.write_text("1")
            time.sleep(1.0)
            log("stopped", app=state.get("foreground") or state.get("selected"), available_mb_before=before,
                available_mb_after=available_mb())

    def _start(self, why):
        before = available_mb()
        self.start()
        self.stopped = False
        MARK.unlink(missing_ok=True)
        log("started", reason=why, available_mb_before=before)


def main():
    watch = Watch(stop=lambda: systemctl("stop", UNITS), start=lambda: systemctl("start", UNITS[::-1]),
                  stopped=MARK.exists())                     # the marker survives a restart of this watcher
    log("watching", mode=settings()["mode"], resumed_with_services_stopped=watch.stopped)
    while True:
        cfg = settings()
        watch.step(launcher_state(), cfg, time.monotonic())
        time.sleep(max(0.3, float(cfg["poll_s"])))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
