#!/usr/bin/env python3
"""Mock of the TARS robot web UI, per docs/TARS_REMOTE_SPEC.md (Flask + flask-socketio, threading mode).

  GET  /login              200 login page
  POST /login              302 to / + session cookie on the right password, 200 (login page again) otherwise
  POST /process_llm        needs the cookie (else 302 to /login); form: message, speaker; returns {"status":"success"}
  GET  /api/system/metrics needs the cookie
  GET  /_received          test helper: every message received (no auth)

After /process_llm it emits bot_stream_start, bot_token..., talking_state, bot_message, [second bot_message],
talking_state(false), then bot_audio_done after --audio-delay seconds. A message containing "error" gives
"Error processing message: ..." then bot_audio_done. A message containing "twice" (or --second-message) gets two
bot_message events. --never-done never sends bot_audio_done.
"""
import argparse, json, secrets, threading, time

from flask import Flask, jsonify, redirect, request, session
from flask_socketio import SocketIO


def make_app(password="changeme", token_delay=0.05, audio_delay=0.5, never_done=False, second_message=False,
             polling_only=False, require_speaker=False, metrics=None, name="mock-tars"):
    app = Flask(name)
    app.secret_key = secrets.token_hex(16)
    kw = dict(async_mode="threading", cors_allowed_origins="*")
    if polling_only:
        kw.update(allow_upgrades=False, transports=["polling"])
    sio = SocketIO(app, **kw)
    state = {"gen": 1, "received": [], "emitted": [], "lock": threading.Lock(), "busy": False, "overlaps": 0, "logins": 0}
    app.mock = state

    def authed():
        return session.get("ok") is True and session.get("gen") == state["gen"]

    @app.get("/login")
    def login_page():
        return "<html><form method=post><input name=password type=password></form></html>", 200

    @app.post("/login")
    def login_post():
        if request.form.get("password") == password:
            session["ok"] = True
            session["gen"] = state["gen"]
            state["logins"] += 1
            return redirect("/")
        return "<html>Wrong password<form method=post></form></html>", 200

    @app.get("/")
    def index():
        return ("ok", 200) if authed() else redirect("/login")

    @app.get("/api/system/metrics")
    def sys_metrics():
        if not authed():
            return redirect("/login")
        return jsonify(metrics if metrics is not None else
                       {"cpu_temp": 51.3, "battery": {"percentage": 87, "voltage": 8.1}, "character": "TARS"})

    @app.get("/_expire")
    def expire():                                           # test helper: invalidates every session cookie
        state["gen"] += 1
        return "expired"

    @app.get("/_received")
    def received():
        return jsonify({"received": state["received"], "overlaps": state["overlaps"], "logins": state["logins"]})

    @app.post("/process_llm")
    def process_llm():
        if not authed():
            return redirect("/login")
        msg, speaker = request.form.get("message", ""), request.form.get("speaker", "")
        with state["lock"]:
            state["received"].append({"message": msg, "speaker": speaker, "t": time.time()})
            if state["busy"]:
                state["overlaps"] += 1                      # a new message arrived before bot_audio_done
            state["busy"] = True
        if require_speaker and speaker != "robot":
            return jsonify({"status": "success"})
        sio.start_background_task(reply, msg)
        return jsonify({"status": "success"})

    def reply(msg):
        emit = lambda ev, data=None: (sio.emit(ev, data if data is not None else {}), state["emitted"].append(ev))
        time.sleep(0.05)
        emit("bot_stream_start")
        if "error" in msg.lower():
            time.sleep(token_delay)
            emit("bot_message", {"message": "Error processing message: simulated failure", "audio_streamed": True})
        else:
            first = f"You said: {msg}"
            for w in first.split(" "):
                emit("bot_token", {"text": w + " "})
                time.sleep(token_delay)
            emit("talking_state", {"talking": True})
            emit("bot_message", {"message": first, "audio_streamed": True})
            if second_message or "twice" in msg.lower():
                time.sleep(audio_delay / 2)
                emit("bot_message", {"message": "Follow-up: I checked and that is all.", "audio_streamed": True})
            time.sleep(audio_delay)
            emit("talking_state", {"talking": False})
        if never_done:
            return
        time.sleep(0.05)
        with state["lock"]:
            state["busy"] = False
        emit("bot_audio_done")

    return app, sio


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--password", default="tars")
    p.add_argument("--token-delay", type=float, default=0.05)
    p.add_argument("--audio-delay", type=float, default=0.5, help="reply delay: seconds of 'speaking' before bot_audio_done")
    p.add_argument("--never-done", action="store_true", help="never send bot_audio_done")
    p.add_argument("--second-message", action="store_true")
    p.add_argument("--polling-only", action="store_true", help="refuse websocket upgrades")
    p.add_argument("--require-speaker", action="store_true", help="stay silent unless speaker=robot")
    a = p.parse_args()
    app, sio = make_app(a.password, a.token_delay, a.audio_delay, a.never_done, a.second_message, a.polling_only, a.require_speaker)
    print(f"mock TARS on http://{a.host}:{a.port}", flush=True)
    sio.run(app, host=a.host, port=a.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
