"""
Giganome — server 3.0 (developed by Rudranil Goswami)
------------------------
Acts purely as a *sync authority and matchmaker*. It never plays audio
itself — it just:
  1. Manages sessions (rooms) identified by a short join code.
  2. Relays tempo / time-signature / start / stop commands from the host
     to everyone else in the room.
  3. Answers clock-sync pings so every client can compute its offset from
     server time. All actual click scheduling happens client-side using
     the Web Audio API, which is far more precise than anything we could
     do by pushing "beat" events over the network one at a time.
  4. Relays WebRTC handshake messages (offer/answer/ICE) between the
     host and each member. Once that handshake completes, the host and
     that member open a direct browser-to-browser connection. If they
     happen to share a Wi-Fi network, that connection runs over the LAN
     with near-zero latency — the host becomes the timing reference for
     that peer instead of this server, automatically, with nothing for
     either person to configure. If a direct connection can't be made
     (different networks, restrictive firewall), everything transparently
     keeps working through this server exactly as before.

Run:
    pip install -r requirements.txt
    python app.py

Then open http://<this-machine's-LAN-IP>:5000 on every device on the
same Wi-Fi network (your phone, your bandmates' laptops, etc).
"""

import random
import string
import time

from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room

import os

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# threading mode + the `simple-websocket` package (in requirements.txt)
# gives real WebSocket support without depending on eventlet/gevent, which
# lag behind newer Python versions. Runs fine locally and in production
# behind gunicorn's gthread worker (see Procfile).
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# code -> session dict
sessions = {}

# sid -> {"code": str, "is_host": bool}
clients = {}

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L — easy to read aloud
START_BUFFER_MS = 1500  # lead time for mid-performance tempo/signature re-anchors
START_COUNT_IN_MS = 10000  # lead time for the initial Start, matching the client's count-in


def now_ms():
    return int(time.time() * 1000)


def generate_code():
    while True:
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(4))
        if code not in sessions:
            return code


def public_state(session):
    """Fields that are safe/relevant to send to a client."""
    return {
        "bpm": session["bpm"],
        "beats_per_bar": session["beats_per_bar"],
        "beat_unit": session["beat_unit"],
        "playing": session["playing"],
        "start_time": session["start_time"],
        "member_count": len(session["members"]),
    }


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    pass


@socketio.on("disconnect")
def on_disconnect():
    from flask import request

    sid = request.sid
    info = clients.pop(sid, None)
    if not info:
        return
    code = info["code"]
    session = sessions.get(code)
    if not session:
        return
    session["members"].discard(sid)

    if info["is_host"]:
        # Host left — end the session for everyone rather than leaving a
        # headless room with no one able to control tempo.
        emit("session_ended", {"reason": "The host left the session."}, room=code)
        sessions.pop(code, None)
    else:
        emit("member_update", {"member_count": len(session["members"])}, room=code)
        # Tell the host so it can tear down that peer's WebRTC connection.
        emit("peer_left", {"sid": sid}, room=session["host_sid"])


@socketio.on("create_session")
def on_create_session(data):
    from flask import request

    sid = request.sid
    code = generate_code()
    bpm = int((data or {}).get("bpm", 120))
    sessions[code] = {
        "host_sid": sid,
        "bpm": max(20, min(300, bpm)),
        "beats_per_bar": 4,
        "beat_unit": 4,
        "playing": False,
        "start_time": None,
        "members": {sid},
    }
    clients[sid] = {"code": code, "is_host": True}
    join_room(code)
    emit("session_created", {"code": code, "is_host": True, **public_state(sessions[code])})


@socketio.on("join_session")
def on_join_session(data):
    from flask import request

    sid = request.sid
    code = (data or {}).get("code", "").strip().upper()
    session = sessions.get(code)
    if not session:
        emit("join_error", {"message": f"No session with code {code}."})
        return

    session["members"].add(sid)
    clients[sid] = {"code": code, "is_host": False}
    join_room(code)
    emit(
        "session_joined",
        {"code": code, "is_host": False, "host_sid": session["host_sid"], **public_state(session)},
    )
    emit("member_update", {"member_count": len(session["members"])}, room=code)
    # Let the host know a new peer showed up so it can open a direct
    # WebRTC connection to them (see webrtc_signal below). Sent only to
    # the host, not the whole room.
    emit("peer_joined", {"sid": sid}, room=session["host_sid"])


def _get_host_session(sid):
    info = clients.get(sid)
    if not info or not info["is_host"]:
        return None
    return sessions.get(info["code"]), info["code"]


@socketio.on("set_tempo")
def on_set_tempo(data):
    from flask import request

    session, code = _get_host_session(request.sid)
    if not session:
        return
    bpm = max(20, min(300, int(data.get("bpm", session["bpm"]))))
    session["bpm"] = bpm

    if session["playing"]:
        # Re-anchor playback so the new tempo takes effect cleanly instead
        # of trying to bend the existing beat grid.
        session["start_time"] = now_ms() + START_BUFFER_MS

    emit(
        "tempo_updated",
        {"bpm": bpm, "playing": session["playing"], "start_time": session["start_time"]},
        room=code,
    )


@socketio.on("set_time_signature")
def on_set_time_signature(data):
    from flask import request

    session, code = _get_host_session(request.sid)
    if not session:
        return
    session["beats_per_bar"] = max(1, min(16, int(data.get("beats_per_bar", session["beats_per_bar"]))))
    session["beat_unit"] = int(data.get("beat_unit", session["beat_unit"]))

    if session["playing"]:
        session["start_time"] = now_ms() + START_BUFFER_MS

    emit(
        "time_signature_updated",
        {
            "beats_per_bar": session["beats_per_bar"],
            "beat_unit": session["beat_unit"],
            "playing": session["playing"],
            "start_time": session["start_time"],
        },
        room=code,
    )


@socketio.on("start")
def on_start(data):
    from flask import request

    session, code = _get_host_session(request.sid)
    if not session:
        return
    session["playing"] = True
    session["start_time"] = now_ms() + START_COUNT_IN_MS
    emit(
        "playback_started",
        {
            "start_time": session["start_time"],
            "bpm": session["bpm"],
            "beats_per_bar": session["beats_per_bar"],
            "beat_unit": session["beat_unit"],
        },
        room=code,
    )


@socketio.on("stop")
def on_stop(data):
    from flask import request

    session, code = _get_host_session(request.sid)
    if not session:
        return
    session["playing"] = False
    session["start_time"] = None
    emit("playback_stopped", {}, room=code)


@socketio.on("webrtc_signal")
def on_webrtc_signal(data):
    """Pure relay for WebRTC handshake messages (offer/answer/ICE candidates)
    between two peers in the same session. The server never looks at or
    stores the payload — it's just a mailbox so two browsers on the same
    Wi-Fi network can find each other and then talk directly, peer to
    peer, without their tempo/clock-sync traffic touching this server at
    all. This is what lets Giganome fall back to near-zero-latency local
    sync automatically whenever devices share a network, with no manual
    hosting step required."""
    from flask import request

    sid = request.sid
    target = (data or {}).get("target")
    info = clients.get(sid)
    target_info = clients.get(target)
    if not info or not target_info or info["code"] != target_info["code"]:
        return  # ignore cross-session or unknown targets

    payload = dict(data or {})
    payload["from"] = sid
    emit("webrtc_signal", payload, room=target)


@socketio.on("sync_ping")
def on_sync_ping(data):
    from flask import request

    # Reply directly to the requester only — this is a 1:1 latency probe,
    # not something the rest of the room needs to see.
    emit("sync_pong", {"t0": (data or {}).get("t0"), "server_time": now_ms()}, room=request.sid)


if __name__ == "__main__":
    # Local development only. In production (Render/Railway), gunicorn's
    # eventlet worker runs this app instead — see Procfile.
    port = int(os.environ.get("PORT", 5000))
    print(f"Giganome server starting on http://0.0.0.0:{port}")
    print("Find this machine's LAN IP (e.g. `ipconfig` / `ifconfig`) and share")
    print(f"http://<that-ip>:{port} with your bandmates on the same Wi-Fi.")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
