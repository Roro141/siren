#!/usr/bin/env python3
"""SOMEBODY relay: Mega serial EVENT lines -> WebSocket -> phone page,
plus Gemini scene-based phrase suggestions.

  python3 server.py [--port /dev/cu.usbmodemXXXX] [--http-port 8080]

Endpoints:
  /            public/index.html
  /ws          WebSocket: broadcasts {"type": "LEFT"|"RIGHT"|"NOD"|"HOME", "server_ms": ...}
               and {"mega": true|false}; answers {"ping": n} with {"pong": n}
  /fake/<T>    broadcast a fake LEFT / RIGHT / NOD / HOME (HOME = reset highlight to start)
  /calibrate   send "c" to the Mega (re-zero; hold head still and neutral)
  /predict     POST {image_b64, recent, local_time} -> Gemini phrase suggestions
  /log         POST {phrase, source, moves_since_last_select, ms_since_first_move} -> trials.csv
  /stats       averages of moves and time per selection, suggested vs fixed

Caregiver dashboard (local network only, never through the public tunnel):
  /dashboard   public/dashboard.html; its WebSocket is /ws?role=dashboard
  /ask         POST {question} -> Gemini answer options -> QUESTION to the phone
  /phrases     GET list / POST {text, category} -> phrases.json, pushed to the phone
  /ack         POST: caregiver "On my way" -> phone speaks "<asker> is coming"

UNO Q mode (--source bridge): no serial port; the sensor app posts to
  /event/<T>       POST from the Bridge app (same as /fake/<T>, source "sensor")
  /heartbeat       POST {ok} every 2 s; reply {"calibrate": true} asks for a re-zero
  /sensor_status   POST {text} sensor messages (calibration, errors)
  /go              plain-HTTP redirect to the current HTTPS tunnel URL (bookmark this)

The Gemini key is read from .env (GEMINI_API_KEY, GEMINI_MODEL). It never
leaves this process. Images are never written to disk.
"""
import argparse
import asyncio
import csv
import glob
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import serial
from aiohttp import WSMsgType, web

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
TRIALS = os.path.join(HERE, "trials.csv")
GESTURES = ("LEFT", "RIGHT", "NOD")
FAKE_TYPES = GESTURES + ("HOME",)
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*")
GRID = [  # never reorder: motor memory. Same order on every device.
    {"text": "I need help", "cat": "help"},
    {"text": "I need water", "cat": "care"},
    {"text": "Bathroom", "cat": "care"},
    {"text": "I'm in pain", "cat": "care"},
    {"text": "Yes", "cat": "neutral"},
    {"text": "No", "cat": "neutral"},
    {"text": "Thank you", "cat": "social"},
]
FIXED_PHRASES = [g["text"] for g in GRID]

PROFILE = os.path.join(HERE, "profile.json")
CUSTOM_PHRASES = os.path.join(HERE, "phrases.json")
MAX_CUSTOM_PHRASES = 4          # phone grid caps at 12 tiles (8 fixed + 4 custom)
DEFAULT_URGENT = ["I need help", "I'm in pain", "Suction please", "I can't breathe"]
BASIC_ANSWERS = ["Yes", "No", "Not right now"]
SOMETHING_ELSE = "Something else"

GEMINI_TIMEOUT_S = 4.0
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PROMPT = (
    "You help a person who cannot speak or use their hands. They communicate by choosing phrases. "
    "Look at the photo from the camera in front of them and suggest the 4 phrases they are most "
    "likely to want to say right now.\n"
    "Rules: first person, natural spoken English, 1-6 words each, practical needs first (drink, food, "
    "comfort, help, people). If a person is visible, include a social phrase (e.g. \"Hi\", \"Thank you\", "
    "\"Can you help me?\"). Never identify or name people, never guess age/gender/ethnicity, never give "
    "medical advice. If the image is dark, blurry or empty, return low confidence and generic helpful phrases.\n"
    "Ignore TV, monitor, laptop and phone screens (and whatever they show) when any other object is visible; "
    "base the phrases on the real objects instead. Only suggest screen-related phrases if a screen is the only "
    "thing in view.\n"
    "Context: recent phrases said = {recent}; local time = {local_time}.\n"
    "Return JSON: {{\"scene\": \"<6-10 word description>\", \"phrases\": [4 strings], \"confidence\": 0.0-1.0}}."
)
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scene": {"type": "STRING"},
        "phrases": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["scene", "phrases", "confidence"],
}

ASK_PROMPT = (
    "You are helping a person with ALS answer a question from their caregiver. They cannot speak or use "
    "their hands; they choose from short options using head movements, so fewer, clearer options are better.\n"
    "Question: \"{question}\"\n"
    "About them: {profile}\n"
    "What the camera currently sees (may be empty): \"{last_scene}\"\n"
    "Recent things they said: {recent}\n"
    "Return JSON: {{\"kind\": \"yes_no\" | \"choice\" | \"open\", \"options\": [3 strings]}}\n"
    "Rules for options: first person, natural spoken English, 1-6 words, distinct from each other, kind and "
    "dignified, never medical advice, never invent facts about people.\n"
    "- yes_no questions: options must be exactly [\"Yes\", \"No\", \"Not right now\"] unless a more specific "
    "third option is clearly better (e.g. \"Yes, but later\").\n"
    "- choice questions (e.g. \"Tea or coffee?\"): the choices offered, plus one sensible alternative.\n"
    "- open questions (e.g. \"What do you want for dinner?\"): 3 specific, likely answers based on the "
    "profile and context."
)
ASK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["yes_no", "choice", "open"]},
        "options": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["kind", "options"],
}

clients = set()        # phone pages
dashboards = set()     # caregiver dashboards
convo = []             # conversation log entries (newest last)
questions = {}         # id -> question record
recent_said = []       # newest first, for Gemini context
alert = None           # {"text", "ts"} while an urgent phrase is unacknowledged
camera_state = {"scene": "", "phrases": [], "ms": None, "ok": None, "reason": "", "ts": None}
health = {"gemini": {"ok": None, "err": ""}, "internet": {"ok": None, "err": ""}}
last_phone_rtt = None
q_counter = 0

# Server-owned state for every talking screen (phone, iPad, ...). Broadcast in full on every change.
state = {
    "highlight": -1,                                   # index into scan_order(); -1 = start
    "suggestions": {"phrases": [], "source": "offline", "scene": ""},
    "question": None,                                  # {"id","question","options","asker"}
    "spoken": None,                                    # {"text","kind","asker","seq"}
    "row_hidden": False,
}
devices = {}            # ws -> {"id", "name"}
active = {"id": None, "name": ""}
last_seen = {}          # device id -> last message time
release_handle = None   # pending release of a disconnected active device
tz_offset_min = 0       # active device's UTC offset (for time-of-day phrases)
last_tilt = 0.0
moves_since_select = 0
first_move_ts = None
pending_sugg = None
pending_question = None
spoken_seq = 0
TALK_PORT = 8080
heard_until = 0.0       # camera results don't replace "heard" replies until this time
HEARD_HOLD_S = 30
ACTIVE_STALE_S = 60     # auto-claim allowed if the active device was silent this long
RELEASE_AFTER_S = 10    # release active after this long disconnected

SOURCE = "serial"           # "serial" (Mega over USB) or "bridge" (UNO Q sensor app)
last_heartbeat = 0.0
calibrate_requested = False
TUNNEL_URL_FILE = os.path.join(HERE, "tunnel_url.txt")
counts = {g: 0 for g in FAKE_TYPES}
loop = None
ser = None
ser_lock = threading.Lock()
mega_connected = False
sensor_ok = False      # the GY-521 is answering (Mega prints "!!" lines when it isn't)
http = None  # aiohttp.ClientSession for Gemini


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def load_env(path=os.path.join(HERE, ".env")):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
GEMINI_KEY = ENV.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = ENV.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"
GEMINI_THINKING = ENV.get("GEMINI_THINKING") or "minimal"
# If a model runs out of free daily quota, fall back to the next one that still has some.
GEMINI_MODELS = list(dict.fromkeys([GEMINI_MODEL] + [m.strip() for m in (ENV.get("GEMINI_FALLBACK_MODELS") or
                     "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3-flash-preview").split(",") if m.strip()]))
gemini_exhausted = {}   # model -> time.time() when its quota should be back
GEMINI_PAUSE_FILE = os.path.join(HERE, "GEMINI_PAUSED")   # touch this file to stop all Gemini calls


def gemini_paused():
    return os.path.exists(GEMINI_PAUSE_FILE)


def gemini_model():
    now = time.time()
    for m in GEMINI_MODELS:
        if gemini_exhausted.get(m, 0) <= now:
            return m
    return min(GEMINI_MODELS, key=lambda m: gemini_exhausted.get(m, 0))


def note_quota(model, status, message):
    """Remember a model that hit its daily quota (HTTP 429 'per day'), so we skip it until it resets."""
    if status != 429 or "quota" not in message.lower():
        return
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?", message)
    secs = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60) if m and (m.group(1) or m.group(2)) else 3600
    gemini_exhausted[model] = time.time() + secs
    log(f"Gemini {model} out of quota for {secs // 60} min; next: {gemini_model()}")


# ---------------------------------------------------------------- serial / websocket
def find_port(forced):
    if forced:
        return forced if os.path.exists(forced) else None
    for pattern in PORT_GLOBS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


async def broadcast(gesture, source):
    counts[gesture] += 1
    log(f"EVENT {gesture:<5} #{counts[gesture]:<3} ({source}) -> {len(clients)} client(s)")
    await handle_gesture(gesture)
    await to_dashboards({"type": gesture, "server_ms": int(time.time() * 1000), "source": source,
                         "phone_rtt": last_phone_rtt})


def headband_ok():
    return mega_connected and sensor_ok


def set_sensor(ok):
    global sensor_ok
    if ok != sensor_ok:
        sensor_ok = ok
        log(f"Sensor: {'responding' if ok else 'NOT responding (check the GY-521 wires)'}")
        if loop:
            asyncio.run_coroutine_threadsafe(send_status(), loop)


async def send_status():
    msg = json.dumps({"mega": headband_ok()})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            clients.discard(ws)
    await to_dashboards({"type": "HEALTH", **health_snapshot()})


async def to_phones(obj):
    msg = json.dumps(obj)
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            clients.discard(ws)


async def to_dashboards(obj):
    msg = json.dumps(obj)
    for ws in list(dashboards):
        try:
            await ws.send_str(msg)
        except Exception:
            dashboards.discard(ws)


def health_snapshot():
    return {
        "sensor": {"ok": headband_ok(),
                   "err": "" if headband_ok() else ("Mega not connected" if not mega_connected
                                                    else "Mega connected, but the tilt sensor (GY-521) is not responding: check its wires")},
        "phone": {"ok": len(clients) > 0, "err": "" if clients else "no talking screen connected",
                  "rtt": last_phone_rtt, "active": active["name"],
                  "devices": sorted({d["name"] for d in devices.values()})},
        "gemini": health["gemini"],
        # TODO(/tts): report the real ElevenLabs status once Test 4 adds /tts.
        "elevenlabs": dict(eleven_health),
        "internet": health["internet"],
        "tunnel": {"ok": bool(tunnel_url()), "url": tunnel_url(), "err": "" if tunnel_url() else "tunnel not up yet"},
        "source": SOURCE,
    }


def set_mega(connected):
    global mega_connected
    mega_connected = connected
    if loop:
        asyncio.run_coroutine_threadsafe(send_status(), loop)


def serial_thread(forced_port):
    global ser
    announced_missing = False
    last_err_log = 0
    while True:
        port = find_port(forced_port)
        if not port:
            if not announced_missing:
                log("Serial: Mega not found, retrying every 2 s...")
                announced_missing = True
            time.sleep(2)
            continue
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
            with ser_lock:
                ser = s
            announced_missing = False
            log(f"Serial: connected to {port} (Mega resets and calibrates now: hold still)")
            set_mega(True)
            buf = b""
            while True:
                buf += s.read(256)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("!!"):
                        set_sensor(False)
                    elif line.startswith((">> Neutral set", "EVENT ", "  still-check")):
                        set_sensor(True)
                    if line.startswith("EVENT "):
                        parts = line.split()
                        if len(parts) > 1 and parts[1] in GESTURES and loop:
                            asyncio.run_coroutine_threadsafe(broadcast(parts[1], "serial"), loop)
                    elif line.startswith("!! Sensor read failed"):
                        if time.time() - last_err_log > 10:      # don't flood the log
                            log(f"Mega: {line} (repeats every second)")
                            last_err_log = time.time()
                    elif line.startswith((">>", "!!", "WHO_AM_I", "SOMEBODY")):
                        log(f"Mega: {line}")
        except (serial.SerialException, OSError) as e:
            log(f"Serial: disconnected ({e.__class__.__name__}), retrying every 2 s...")
        finally:
            set_sensor(False)
            with ser_lock:
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                ser = None
            set_mega(False)
        time.sleep(2)


NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}   # phones must never run a stale page after an update


async def index(_request):
    return web.FileResponse(os.path.join(PUBLIC, "index.html"), headers=NO_CACHE)


async def fake(request):
    gesture = request.match_info["gesture"].upper()
    if gesture not in FAKE_TYPES:
        raise web.HTTPNotFound(text="use LEFT, RIGHT, NOD or HOME\n")
    await broadcast(gesture, "fake")
    return web.Response(text=f"sent fake {gesture}\n")


async def calibrate(_request):
    global calibrate_requested
    if SOURCE == "bridge":
        if not mega_connected:
            return web.Response(status=503, text="sensor app not connected\n")
        calibrate_requested = True
        log("Calibrate requested (sent with the next heartbeat; hold still)")
        return web.Response(text="calibrating\n")
    with ser_lock:
        if not ser:
            return web.Response(status=503, text="Mega not connected\n")
        ser.write(b"c\n")
    log("Sent calibrate to Mega (hold still)")
    return web.Response(text="calibrating\n")


async def ws_handler(request):
    if request.query.get("role") == "dashboard":
        return await dashboard_ws(request)
    global last_phone_rtt, release_handle
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    dev_id = (request.query.get("device") or f"anon-{id(ws)}")[:64]
    dev_name = (request.query.get("name") or "Device")[:32]
    devices[ws] = {"id": dev_id, "name": dev_name}
    last_seen[dev_id] = time.time()
    clients.add(ws)
    log(f"Page connected: {dev_name} [{dev_id[:6]}] ({len(clients)} client(s))")
    if dev_id == active["id"] and release_handle:
        release_handle.cancel()
        release_handle = None
        log(f"Active device {dev_name} reconnected")
    await ws.send_str(json.dumps({"mega": headband_ok()}))
    await ws.send_str(json.dumps(public_state()))
    await to_dashboards({"type": "HEALTH", **health_snapshot()})
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                last_seen[dev_id] = time.time()
                kind = data.get("type")
                if "ping" in data:
                    await ws.send_str(json.dumps({"pong": data["ping"]}))
                elif "rtt" in data:
                    if dev_id == active["id"]:
                        last_phone_rtt = data["rtt"]
                elif kind == "CLAIM":
                    await claim(dev_id, dev_name, bool(data.get("force")), data.get("tz"))
                elif kind == "INPUT" and data.get("gesture") in FAKE_TYPES:
                    await broadcast(data["gesture"], f"keys:{dev_name}")
                elif kind == "TAP":
                    try:
                        await select_index(int(data.get("index")), via="tap")
                    except (TypeError, ValueError):
                        pass
                elif kind == "CAMERA" and dev_id == active["id"] and not data.get("on"):
                    go_offline()
                elif kind == "ROW":
                    state["row_hidden"] = bool(data.get("hidden"))
                    state["highlight"] = -1
                    await push_state()
                elif kind == "SHOWN" and dev_id == active["id"]:
                    await phone_message(data)
    finally:
        clients.discard(ws)
        devices.pop(ws, None)
        log(f"Page disconnected: {dev_name} ({len(clients)} client(s))")
        if dev_id == active["id"] and not any(d["id"] == dev_id for d in devices.values()):
            schedule_release(dev_id)
        await to_dashboards({"type": "HEALTH", **health_snapshot()})
    return ws


# ---------------------------------------------------------------- shared state machine
def grid_items():
    custom = [{"text": p["text"], "cat": p.get("category", "neutral")} for p in load_custom_phrases()]
    return (GRID + custom)[:12]


def row_visible():
    return bool(state["question"]) or not state["row_hidden"]


def scan_order():
    items = []
    if state["question"]:
        opts = (state["question"]["options"] + [""] * 4)[:4]
        items += [{"text": o, "source": "answer"} for o in opts]
    elif not state["row_hidden"]:
        phr = (state["suggestions"]["phrases"] + [""] * 4)[:4]
        items += [{"text": p, "source": "suggested"} for p in phr]
    items += [{"text": g["text"], "source": "fixed"} for g in grid_items()]
    return items


def public_state():
    return {"type": "STATE", "highlight": state["highlight"], "suggestions": state["suggestions"],
            "question": state["question"], "spoken": state["spoken"], "row_hidden": state["row_hidden"],
            "grid": grid_items(), "active": dict(active), "mega": headband_ok(),
            "voice": voice_public(), "listen": listen_state}


async def push_state():
    await to_phones(public_state())


async def claim(dev_id, dev_name, force, tz=None):
    global release_handle, tz_offset_min
    cur = active["id"]
    stale = cur is None or time.time() - last_seen.get(cur, 0) > ACTIVE_STALE_S \
        or not any(d["id"] == cur for d in devices.values()) and release_handle is None
    if cur == dev_id or force or stale:
        if cur != dev_id:
            log(f"Active device: {dev_name} [{dev_id[:6]}] ({'tap' if force else 'auto'}; was {active['name'] or 'none'})")
        active.update({"id": dev_id, "name": dev_name})
        if release_handle:
            release_handle.cancel()
            release_handle = None
        try:
            tz_offset_min = int(tz)
        except (TypeError, ValueError):
            pass
        await push_state()
        await to_dashboards({"type": "HEALTH", **health_snapshot()})


def schedule_release(dev_id):
    global release_handle

    def release():
        global release_handle
        release_handle = None
        if active["id"] == dev_id and not any(d["id"] == dev_id for d in devices.values()):
            log(f"Active device {active['name']} gone for {RELEASE_AFTER_S} s: released")
            active.update({"id": None, "name": ""})
            asyncio.ensure_future(push_state())
            asyncio.ensure_future(to_dashboards({"type": "HEALTH", **health_snapshot()}))

    if release_handle:
        release_handle.cancel()
    release_handle = loop.call_later(RELEASE_AFTER_S, release)


async def handle_gesture(g):
    global last_tilt, moves_since_select, first_move_ts
    now = time.time()
    if g == "HOME":
        state["highlight"] = -1
        moves_since_select, first_move_ts = 0, None
    elif g in ("LEFT", "RIGHT"):
        last_tilt = now
        moves_since_select += 1
        if first_move_ts is None:
            first_move_ts = now
        n = len(scan_order())
        h = state["highlight"]
        if g == "RIGHT":
            h = 0 if h < 0 else (h + 1) % n
        else:
            h = n - 1 if h < 0 else (h - 1 + n) % n
        state["highlight"] = h
    elif g == "NOD":
        last_tilt = now
        if state["highlight"] >= 0:
            await select_index(state["highlight"], via="nod")
            return
    await push_state()


async def select_index(i, via):
    """Select scan-order item i (NOD on the highlight, or a tap on a tile)."""
    global spoken_seq, moves_since_select, first_move_ts, alert
    items = scan_order()
    if not (0 <= i < len(items)) or not items[i]["text"]:
        return
    item = items[i]
    text = item["text"]
    spoken_seq += 1
    q = state["question"]
    if item["source"] == "answer" and q:
        rec = questions.get(q["id"])
        if rec and rec.get("answer") is None:
            rec["answer"] = text
            rec["answer_ms"] = int(time.time() * 1000) - rec["sent_ts"]
            rec["status"] = "answered"
            log(f"ANSWER {q['id']}: {text!r} after {rec['answer_ms']} ms")
            await to_dashboards({"type": "QUESTION_UPDATE", "question": rec})
        await add_convo(text, "answer", qid=q["id"])
        state["question"] = None
        state["highlight"] = -1
        kind = "answer"
    else:
        source = "tap" if via == "tap" else ("grid" if item["source"] == "fixed" else "suggested")
        await add_convo(text, source)
        if via == "nod":
            write_trial(text, item["source"], moves_since_select,
                        int((time.time() - first_move_ts) * 1000) if first_move_ts else 0)
        if _norm(text) in {_norm(u) for u in urgent_list()}:
            alert = {"text": text, "ts": int(time.time() * 1000)}
            log(f"URGENT: {text!r}")
            notify_watch(text)
            await to_dashboards({"type": "ALERT", "alert": alert})
        kind = "said"
    if text != SOMETHING_ELSE:
        recent_said.insert(0, text)
        del recent_said[5:]
    moves_since_select, first_move_ts = 0, None
    state["spoken"] = {"text": text, "kind": kind, "asker": "", "seq": spoken_seq}
    await to_phones({"type": "SELECT", "index": i, "text": text, "seq": spoken_seq})
    await push_state()


def offline_phrases():
    t = datetime.now(timezone.utc) + timedelta(minutes=tz_offset_min)
    m = t.hour * 60 + t.minute
    out = []
    if 5 * 60 <= m < 12 * 60:
        out.append("Good morning")
    if 11 * 60 + 30 <= m <= 13 * 60 + 30 or 17 * 60 + 30 <= m <= 19 * 60 + 30:
        out.append("I'm hungry")
    if m >= 21 * 60 or m < 5 * 60:
        out.append("I'm tired")
    out += [r for r in recent_said[:2] if _norm(r) not in FIXED_NORM]
    out += ["Can you help me?", "Please come here", "I'm cold", "Please wait"]
    seen, res = set(), []
    for p in out:
        if _norm(p) not in seen:
            seen.add(_norm(p))
            res.append(p)
    return res[:4]


def offer_suggestions(phrases, source, scene=""):
    """Queue new suggestions. Stable slots: phrases still suggested keep their tile."""
    global pending_sugg
    seen = {p.lower() for p in phrases}
    incoming = (list(phrases) + [p for p in offline_phrases() if p.lower() not in seen])[:4]
    cur = state["suggestions"]["phrases"] if source != "heard" else []
    keep = [p if any(q.lower() == p.lower() for q in incoming) else None for p in cur]
    fresh = [q for q in incoming if not any(p and p.lower() == q.lower() for p in keep)]
    merged = [(p or (fresh.pop(0) if fresh else "")) for p in keep] + fresh
    merged = (merged + [""] * 4)[:4] if source == "heard" else merged[:4]
    if merged == cur and source == state["suggestions"]["source"]:
        pending_sugg = None
        if scene and scene != state["suggestions"].get("scene"):
            state["suggestions"]["scene"] = scene
            asyncio.ensure_future(push_state())
        return
    pending_sugg = {"phrases": merged, "source": source, "scene": scene}


def go_offline():
    offer_suggestions(offline_phrases(), "offline", "")


async def scheduler():
    """Apply queued questions/suggestions only while the user is not moving."""
    global pending_sugg, pending_question
    while True:
        await asyncio.sleep(0.2)
        idle = time.time() - last_tilt
        if pending_question and idle >= 1.0:
            q, pending_question = pending_question, None
            state["question"] = q
            state["highlight"] = 0
            state["spoken"] = {"text": q["question"], "kind": "heard" if q.get("mode") == "listen" else "question",
                               "asker": q["asker"], "seq": spoken_seq}
            asyncio.ensure_future(expire_question(q["id"]))
            await push_state()
        if pending_sugg and idle >= 3.0:
            state["suggestions"], pending_sugg = pending_sugg, None
            warm_cache(state["suggestions"]["phrases"])
            await push_state()


async def expire_question(qid):
    await asyncio.sleep(180)
    if state["question"] and state["question"]["id"] == qid:
        rec = questions.get(qid)
        if rec and rec.get("answer") is None:
            rec["status"] = "expired"
            await to_dashboards({"type": "QUESTION_UPDATE", "question": rec})
        log(f"QUESTION {qid} expired unanswered")
        state["question"] = None
        state["highlight"] = -1
        await push_state()


def write_trial(phrase, source, moves, ms):
    new = not os.path.exists(TRIALS)
    with open(TRIALS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"ts": datetime.now().isoformat(timespec="seconds"), "phrase": phrase[:80],
                    "source": "suggested" if source == "suggested" else "fixed",
                    "moves_since_last_select": moves, "ms_since_first_move": ms})


# ---------------------------------------------------------------- dashboard / conversation
def is_local(request):
    """True unless the request came in through the public Cloudflare tunnel."""
    return "Cf-Connecting-Ip" not in request.headers and "Cf-Ray" not in request.headers


def local_only(handler):
    async def wrapped(request):
        if not is_local(request):
            return web.Response(status=403, text="The dashboard is only available on the local network.\n")
        return await handler(request)
    return wrapped


def load_profile():
    try:
        with open(PROFILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def names():
    prof = load_profile()
    return prof.get("name") or "them", prof.get("caregiver_name") or ""


ALERTS_ON = (ENV.get("SIRENIZE_ALERTS") or "on").lower() not in ("off", "0", "false", "no")


def urgent_list():
    """Phrases that raise the dashboard's urgent banner. Empty when SIRENIZE_ALERTS=off in .env."""
    if not ALERTS_ON:
        return []
    lst = load_profile().get("urgent_phrases")
    return [u for u in lst if isinstance(u, str)] if isinstance(lst, list) and lst else DEFAULT_URGENT


def notify_watch(text):
    # TODO(watch): push to the caregiver's Apple Watch. For now this only logs.
    log(f"NOTIFY_WATCH (stub): {text!r}")


CONVO_CSV = os.path.join(HERE, "conversation.csv")


async def add_convo(text, source, **extra):
    entry = {"ts": int(time.time() * 1000), "text": text, "source": source,
             "urgent": source not in ("question", "heard", "caregiver") and _norm(text) in {_norm(u) for u in urgent_list()},
             **extra}
    convo.append(entry)
    del convo[:-300]
    new = not os.path.exists(CONVO_CSV)
    with open(CONVO_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "phrase", "how", "urgent"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), text, source,
                    "yes" if _norm(text) in {_norm(u) for u in urgent_list()} else ""])
    await to_dashboards({"type": "SUMMARY", "summary": data_summary()})
    await to_dashboards({"type": "CONVO", "entry": entry})
    return entry


async def phone_message(data):
    global alert
    kind = data.get("type")
    if kind == "SAID":
        phrase = str(data.get("phrase", ""))[:120]
        source = data.get("source") if data.get("source") in ("grid", "suggested", "tap") else "grid"
        if not phrase:
            return
        recent_said.insert(0, phrase)
        del recent_said[5:]
        await add_convo(phrase, source)
        if _norm(phrase) in {_norm(u) for u in urgent_list()}:
            alert = {"text": phrase, "ts": int(time.time() * 1000)}
            log(f"URGENT: {phrase!r}")
            notify_watch(phrase)
            await to_dashboards({"type": "ALERT", "alert": alert})
        return
    q = questions.get(str(data.get("id")))
    if not q:
        return
    now = int(time.time() * 1000)
    if kind == "SHOWN" and q.get("shown_ms") is None:
        q["shown_ms"] = now - q["sent_ts"]
        log(f"QUESTION {q['id']} on phone after {q['shown_ms']} ms")
    elif kind == "ANSWER" and q.get("answer") is None:
        q["answer"] = str(data.get("answer", ""))[:120]
        q["answer_ms"] = now - q["sent_ts"]
        q["status"] = "answered"
        if q["answer"] != SOMETHING_ELSE:
            recent_said.insert(0, q["answer"])
            del recent_said[5:]
        log(f"ANSWER {q['id']}: {q['answer']!r} after {q['answer_ms']} ms")
        await add_convo(q["answer"], "answer", qid=q["id"])
    elif kind == "EXPIRED" and q.get("answer") is None:
        q["status"] = "expired"
        log(f"QUESTION {q['id']} expired unanswered")
    await to_dashboards({"type": "QUESTION_UPDATE", "question": q})


async def dashboard_ws(request):
    if not is_local(request):
        return web.Response(status=403, text="local network only\n")
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    dashboards.add(ws)
    name, caregiver = names()
    log(f"Dashboard connected ({len(dashboards)})")
    await ws.send_str(json.dumps({
        "type": "INIT", "name": name, "conversation": convo[-200:],
        "alert": alert, "health": health_snapshot(), "summary": data_summary(),
    }))
    try:
        async for _ in ws:
            pass
    finally:
        dashboards.discard(ws)
    return ws


def static_file(name, ctype):
    async def handler(_request):
        return web.FileResponse(os.path.join(PUBLIC, name), headers={"Content-Type": ctype})
    return handler


async def dashboard_page(_request):
    return web.FileResponse(os.path.join(PUBLIC, "dashboard.html"), headers=NO_CACHE)


async def gemini_json(parts, schema):
    """Gemini call with JSON output. Retries once, fast, on 503/429 if time is left in the budget."""
    t0 = time.time()
    try:
        return await _gemini_json_once(parts, schema, GEMINI_TIMEOUT_S)
    except RuntimeError as e:
        left = GEMINI_TIMEOUT_S - (time.time() - t0)
        if not str(e).startswith(("Gemini HTTP 503", "Gemini HTTP 429")) or left < 1.0:
            raise
        log(f"Gemini busy ({str(e)[:40]}), retrying once with {left:.1f} s left")
        data, _ = await _gemini_json_once(parts, schema, left)
        return data, int((time.time() - t0) * 1000)


async def _gemini_json_once(parts, schema, timeout_s):
    if not GEMINI_KEY:
        raise RuntimeError("no GEMINI_API_KEY in .env")
    if gemini_paused():
        raise RuntimeError("Gemini paused")
    t0 = time.time()
    model = gemini_model()
    payload = {"contents": [{"parts": parts}], "generationConfig": {
        "responseMimeType": "application/json", "responseSchema": schema,
        "thinkingConfig": {"thinkingLevel": GEMINI_THINKING}}}
    try:
        async with http.post(GEMINI_URL.format(model=model), json=payload,
                             headers={"x-goog-api-key": GEMINI_KEY},
                             timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            text = await r.text()
            if r.status != 200:
                try:
                    msg = json.loads(text)["error"]["message"]
                except Exception:
                    msg = text[:200]
                note_quota(model, r.status, msg)
                raise RuntimeError(f"Gemini HTTP {r.status}: {msg}".replace(GEMINI_KEY, "<KEY>"))
        data = json.loads(json.loads(text)["candidates"][0]["content"]["parts"][0]["text"])
    except asyncio.TimeoutError:
        raise RuntimeError(f"timeout after {GEMINI_TIMEOUT_S:.0f} s")
    except (aiohttp.ClientError, OSError) as e:
        raise RuntimeError(f"network: {e.__class__.__name__}")
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise RuntimeError(f"bad Gemini response: {e.__class__.__name__}")
    return data, int((time.time() - t0) * 1000)


def clean_options(raw, kind="yes_no"):
    out, seen = [], set()
    for o in raw if isinstance(raw, list) else []:
        if not isinstance(o, str):
            continue
        o = " ".join(o.strip().strip('"').split()).rstrip(".")
        n = _norm(o)
        if not (1 <= len(n.split()) <= 6) or n in seen or n == _norm(SOMETHING_ELSE):
            continue
        seen.add(n)
        out.append(o)
    pads = BASIC_ANSWERS if kind == "yes_no" else ["I'm not sure", "Not right now", "No thank you"]
    for b in pads:                             # pad to exactly 3 if Gemini gave too few
        if len(out) >= 3:
            break
        if _norm(b) not in seen:
            seen.add(_norm(b))
            out.append(b)
    return out[:3]


async def ask(request):
    try:
        question = " ".join(str((await request.json())["question"]).split())[:200]
    except Exception:
        return web.json_response({"ok": False, "reason": "need JSON with question"}, status=400)
    if not question:
        return web.json_response({"ok": False, "reason": "empty question"}, status=400)
    q = await ask_core(question, mode="ask")
    return web.json_response({"ok": True, "question": q, "phones": len(clients)})


async def ask_core(question, mode="ask", extra=None):
    """Gemini -> 3 reply options + "Something else", queued for the talking screen.
    mode "ask": typed by the caregiver. mode "listen": transcribed from someone speaking."""
    global q_counter, pending_question
    t0 = time.time()
    q_counter += 1
    qid = f"q{int(t0)}-{q_counter}"
    name, caregiver = names()
    who = (caregiver or "Caregiver") if mode == "ask" else "Someone"
    await add_convo(question, "question" if mode == "ask" else "heard", qid=qid, asker=who)
    base_prompt = ASK_PROMPT if mode == "ask" else ASK_PROMPT.replace(
        "answer a question from their caregiver", "reply to something a person just said to them out loud").replace(
        'Question: "{question}"', 'What they said (speech transcript, may be a statement or a question): "{question}"')
    prompt = base_prompt.format(question=question.replace('"', "'"),
                               profile=json.dumps(load_profile(), ensure_ascii=False),
                               last_scene=camera_state.get("scene", ""), recent=json.dumps(recent_said))
    kind, fallback, reason, gem_ms = "yes_no", False, "", None
    try:
        data, gem_ms = await gemini_json([{"text": prompt}], ASK_SCHEMA)
        kind = data.get("kind") if data.get("kind") in ("yes_no", "choice", "open") else "open"
        options = clean_options(data.get("options"), kind)
        health["gemini"] = {"ok": True, "err": ""}
    except RuntimeError as e:
        reason = str(e)
        fallback = True
        options = list(BASIC_ANSWERS)
        health["gemini"] = {"ok": False, "err": reason}
        log(f"ASK fallback: {reason}")
    options = options + [SOMETHING_ELSE]
    warm_cache(options)                         # prefetch the 4 replies in the user's voice
    q = {"id": qid, "question": question, "kind": kind, "options": options, "fallback": fallback,
         "reason": reason, "gemini_ms": gem_ms, "sent_ts": int(time.time() * 1000), "shown_ms": None,
         "answer": None, "answer_ms": None, "status": "waiting", "mode": mode, **(extra or {})}
    questions[qid] = q
    while len(questions) > 50:
        questions.pop(next(iter(questions)))
    pending_question = {"id": qid, "question": question, "options": options,
                        "asker": who if mode == "ask" else "", "mode": mode}
    await to_dashboards({"type": "QUESTION_UPDATE", "question": q})
    await to_dashboards({"type": "HEALTH", **health_snapshot()})
    log(f"ASK[{mode}] {qid} ({int((time.time()-t0)*1000)} ms, gemini {gem_ms} ms) {question!r} -> {options}")
    return q


async def ack(_request):
    global alert, spoken_seq
    name, caregiver = names()
    alert = None
    reply = f"{caregiver} is coming" if caregiver else "Help is coming"
    await to_dashboards({"type": "ALERT_CLEAR"})
    spoken_seq += 1
    state["spoken"] = {"text": reply, "kind": "message", "asker": "", "seq": spoken_seq}
    await to_phones({"type": "SAY", "text": reply, "seq": spoken_seq})
    await push_state()
    await add_convo(reply, "caregiver")
    log(f"ACK: phone says {reply!r}")
    return web.json_response({"ok": True, "reply": reply})


def load_custom_phrases():
    try:
        with open(CUSTOM_PHRASES, encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, dict) and isinstance(p.get("text"), str)]
    except (OSError, ValueError):
        return []


async def get_phrases(_request):
    return web.json_response(load_custom_phrases())


async def add_phrase(request):
    try:
        d = await request.json()
        text = " ".join(str(d["text"]).split())
        category = d.get("category") if d.get("category") in ("care", "comfort", "social", "neutral", "help") else "neutral"
    except Exception:
        return web.json_response({"ok": False, "reason": "need JSON with text"}, status=400)
    phrases = load_custom_phrases()
    existing = {_norm(p) for p in FIXED_PHRASES} | {_norm(p["text"]) for p in phrases}
    if not (1 <= len(text.split()) <= 8) or len(text) > 40:
        return web.json_response({"ok": False, "reason": "use 1-8 words, at most 40 characters"})
    if _norm(text) in existing:
        return web.json_response({"ok": False, "reason": "that phrase is already on the grid"})
    if len(phrases) >= MAX_CUSTOM_PHRASES:
        return web.json_response({"ok": False, "reason": f"the grid is full ({MAX_CUSTOM_PHRASES} custom phrases max)"})
    phrases.append({"text": text, "category": category})
    with open(CUSTOM_PHRASES, "w", encoding="utf-8") as f:
        json.dump(phrases, f, indent=2, ensure_ascii=False)
    # TODO(/tts): generate and cache the ElevenLabs clip here, then report "Ready in her voice".
    await push_state()
    await to_dashboards({"type": "PHRASES", "phrases": phrases})
    log(f"PHRASE added: {text!r} ({category})")
    return web.json_response({"ok": True, "phrases": phrases,
                              "voice": "device voice (ElevenLabs not set up yet)"})


def tunnel_url():
    try:
        with open(TUNNEL_URL_FILE) as f:
            url = f.read().strip()
        return url if url.startswith("https://") else ""
    except OSError:
        return ""


def lan_ip():
    import socket
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("1.1.1.1", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return ""


async def links(request):
    import socket
    port = TALK_PORT
    host = socket.gethostname()
    return web.json_response({"go": f"http://{host}.local:{port}/go",
                              "go_ip": f"http://{lan_ip()}:{port}/go" if lan_ip() else "",
                              "tunnel": tunnel_url()})


async def go(_request):
    url = tunnel_url()
    if url:
        raise web.HTTPFound(url)
    return web.Response(content_type="text/html", text=(
        "<!doctype html><meta name=viewport content='width=device-width'><meta http-equiv=refresh content=3>"
        "<body style='font:20px -apple-system;background:#121417;color:#F2F4F7;padding:24px'>"
        "Starting up... this page retries every 3 seconds.</body>"))


async def bridge_event(request):
    gesture = request.match_info["gesture"].upper()
    if gesture not in GESTURES:
        raise web.HTTPNotFound(text="use LEFT, RIGHT or NOD\n")
    mark_heartbeat(True)
    await broadcast(gesture, "sensor")
    return web.json_response({"ok": True})


def mark_heartbeat(ok):
    global last_heartbeat, mega_connected
    last_heartbeat = time.time()
    if not mega_connected:
        mega_connected = True
        log("Sensor app: connected")
    set_sensor(bool(ok))
    if loop:
        asyncio.ensure_future(send_status())


async def heartbeat(request):
    global calibrate_requested
    try:
        ok = bool((await request.json()).get("ok"))
    except Exception:
        ok = False
    mark_heartbeat(ok)
    reply = {"ok": True, "calibrate": calibrate_requested}
    calibrate_requested = False
    return web.json_response(reply)


async def sensor_status(request):
    try:
        text = str((await request.json()).get("text", ""))[:200]
    except Exception:
        return web.json_response({"ok": False}, status=400)
    log(f"Sensor: {text}")
    if text.startswith("!!"):
        set_sensor(False)
    elif text.startswith(("Neutral set", "GY-521 found")):
        set_sensor(True)
    return web.json_response({"ok": True})


async def heartbeat_watchdog():
    global mega_connected
    while True:
        await asyncio.sleep(1)
        if mega_connected and time.time() - last_heartbeat > 6:
            mega_connected = False
            log("Sensor app: no heartbeat for 6 s, marking sensor offline")
            set_sensor(False)
            await send_status()


async def internet_loop():
    while True:
        try:
            async with http.get("https://www.gstatic.com/generate_204",
                                timeout=aiohttp.ClientTimeout(total=3)) as r:
                health["internet"] = {"ok": r.status == 204, "err": "" if r.status == 204 else f"HTTP {r.status}"}
        except Exception as e:
            health["internet"] = {"ok": False, "err": e.__class__.__name__}
        await to_dashboards({"type": "HEALTH", **health_snapshot()})
        await asyncio.sleep(10)


# ---------------------------------------------------------------- Gemini predictions
def _norm(p):
    return re.sub(r"[^a-z0-9' ]", "", p.lower().replace("’", "'")).strip()


FIXED_NORM = {_norm(p) for p in FIXED_PHRASES}


def clean_phrases(raw):
    """1-6 words, deduplicated, not already on the fixed grid, at most 4."""
    out, seen = [], set()
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, str):
            continue
        p = " ".join(p.strip().strip('"').split())
        n = _norm(p)
        words = len(n.split())
        if not (1 <= words <= 6) or n in seen or n in FIXED_NORM:
            continue
        seen.add(n)
        out.append(p)
        if len(out) == 4:
            break
    return out


async def predict(request):
    t0 = time.time()
    dev = request.headers.get("X-Device", "")
    if dev and active["id"] and dev != active["id"]:
        return web.json_response({"ok": False, "reason": "not the active device", "ms": 0})
    if gemini_paused():
        return web.json_response({"ok": False, "reason": "Gemini paused", "ms": 0})

    def fail(reason, status=None):
        ms = int((time.time() - t0) * 1000)
        log(f"PREDICT fail {ms} ms: {reason}")
        if reason.startswith(("Gemini", "timeout", "network", "bad Gemini", "no GEMINI")):
            health["gemini"] = {"ok": False, "err": reason}
            camera_state.update({"ok": False, "reason": reason, "ms": ms, "ts": int(time.time() * 1000)})
            asyncio.ensure_future(to_dashboards({"type": "CAMERA", "camera": camera_state}))
            go_offline()
        out = {"ok": False, "reason": reason, "ms": ms}
        if status:
            out["status"] = status
        return web.json_response(out)

    if not GEMINI_KEY:
        return fail("no GEMINI_API_KEY in .env")
    try:
        body = await request.json()
        img = body["image_b64"]
    except Exception:
        return fail("bad request: need JSON with image_b64")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    recent = [str(r) for r in (body.get("recent") or [])][:5]
    local_time = str(body.get("local_time") or datetime.now().strftime("%H:%M"))

    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": img}},
            {"text": PROMPT.format(recent=json.dumps(recent), local_time=local_time)},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": GEMINI_THINKING},
        },
    }
    try:
        model = gemini_model()
        async with http.post(
            GEMINI_URL.format(model=model),
            json=payload,
            headers={"x-goog-api-key": GEMINI_KEY},
            timeout=aiohttp.ClientTimeout(total=GEMINI_TIMEOUT_S),
        ) as r:
            text = await r.text()
            if r.status != 200:
                try:
                    msg = json.loads(text)["error"]["message"]
                except Exception:
                    msg = text[:200]
                note_quota(model, r.status, msg)
                return fail(f"Gemini HTTP {r.status}: {msg}".replace(GEMINI_KEY, "<KEY>"), r.status)
            data = json.loads(text)
        out = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
    except asyncio.TimeoutError:
        return fail(f"timeout after {GEMINI_TIMEOUT_S:.0f} s")
    except (aiohttp.ClientError, OSError) as e:
        return fail(f"network: {e.__class__.__name__}")
    except (KeyError, IndexError, ValueError, TypeError) as e:
        return fail(f"bad Gemini response: {e.__class__.__name__}")

    phrases = clean_phrases(out.get("phrases"))
    scene = " ".join(str(out.get("scene", "")).split())[:120]
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    ms = int((time.time() - t0) * 1000)
    log(f"PREDICT {ms} ms conf={conf:.2f} scene={scene!r} phrases={phrases}")
    health["gemini"] = {"ok": True, "err": ""}
    camera_state.update({"scene": scene, "phrases": phrases, "ms": ms, "ok": True, "reason": "",
                         "confidence": conf, "ts": int(time.time() * 1000)})
    await to_dashboards({"type": "CAMERA", "camera": camera_state})
    if phrases and conf >= 0.4 and time.time() >= heard_until:
        offer_suggestions(phrases, "camera", scene)
    elif not state["suggestions"]["phrases"]:
        go_offline()
    if not phrases:
        return web.json_response({"ok": False, "reason": "no valid phrases", "scene": scene, "ms": ms})
    return web.json_response({"ok": True, "scene": scene, "phrases": phrases, "confidence": conf, "ms": ms})


# ---------------------------------------------------------------- trial logging
CSV_FIELDS = ["ts", "phrase", "source", "moves_since_last_select", "ms_since_first_move"]


async def log_trial(request):
    try:
        d = await request.json()
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "phrase": str(d["phrase"])[:80],
            "source": "suggested" if d.get("source") == "suggested" else "fixed",
            "moves_since_last_select": int(d.get("moves_since_last_select", 0)),
            "ms_since_first_move": int(d.get("ms_since_first_move", 0)),
        }
    except Exception:
        return web.json_response({"ok": False, "reason": "bad request"}, status=400)
    new = not os.path.exists(TRIALS)
    with open(TRIALS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    log(f"TRIAL {row['source']:<9} moves={row['moves_since_last_select']} "
        f"ms={row['ms_since_first_move']} phrase={row['phrase']!r}")
    return web.json_response({"ok": True})


async def stats(_request):
    agg = {}
    if os.path.exists(TRIALS):
        with open(TRIALS, newline="") as f:
            for row in csv.DictReader(f):
                a = agg.setdefault(row["source"], {"n": 0, "moves": 0, "ms": 0})
                a["n"] += 1
                a["moves"] += int(row["moves_since_last_select"])
                a["ms"] += int(row["ms_since_first_move"])
    out = {src: {"n": a["n"],
                 "avg_moves": round(a["moves"] / a["n"], 2),
                 "avg_seconds": round(a["ms"] / a["n"] / 1000, 2)}
           for src, a in agg.items()}
    return web.json_response(out)


# ---------------------------------------------------------------- ElevenLabs: voice + listen
ELEVEN_KEY = ENV.get("ELEVENLABS_API_KEY", "")
ELEVEN = "https://api.elevenlabs.io"
DEFAULT_VOICE_ID = ENV.get("ELEVENLABS_DEFAULT_VOICE") or "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_VOICE_NAME = ENV.get("ELEVENLABS_DEFAULT_VOICE_NAME") or "Default voice"
TTS_MODEL = ENV.get("ELEVENLABS_TTS_MODEL") or "eleven_flash_v2_5"
STT_MODEL = ENV.get("ELEVENLABS_STT_MODEL") or "scribe_v2"
VOICE_FILE = os.path.join(HERE, "voice.json")            # USER_VOICE_ID lives here (server only)
AUDIO_CACHE = os.path.join(HERE, "audio_cache")
VOICE_TMP = os.path.join(HERE, "voice_tmp")              # raw samples, deleted after cloning
SAMPLE_PHRASES = ["Hi, it's me.", "I love you.", "I need help."]
eleven_health = {"ok": None if ELEVEN_KEY else False, "err": "" if ELEVEN_KEY else "no ELEVENLABS_API_KEY in .env"}
listen_state = {"on": False}
warm_sem = None
regen = {"running": False, "done": 0, "total": 0}


def eleven_headers(json_body=False):
    h = {"xi-api-key": ELEVEN_KEY}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def eleven_error(status, text):
    try:
        d = json.loads(text).get("detail")
        msg = d.get("message") if isinstance(d, dict) else str(d)
    except Exception:
        msg = text[:200]
    return f"ElevenLabs HTTP {status}: {msg}".replace(ELEVEN_KEY, "<KEY>") if ELEVEN_KEY else msg


def load_voice():
    try:
        with open(VOICE_FILE) as f:
            v = json.load(f)
        return v if v.get("voice_id") else None
    except (OSError, ValueError):
        return None


def current_voice_id():
    v = load_voice()
    return v["voice_id"] if v else DEFAULT_VOICE_ID


def voice_public():
    v = load_voice()
    return {"id": current_voice_id(), "name": v["name"] if v else DEFAULT_VOICE_NAME,
            "is_user": bool(v), "source": v.get("source") if v else "default",
            "created": v.get("created") if v else None, "tts": bool(ELEVEN_KEY)}


def cache_path(voice_id, text):
    import hashlib
    h = hashlib.sha1(f"{TTS_MODEL}|{text}".encode()).hexdigest()[:20]
    return os.path.join(AUDIO_CACHE, voice_id, h + ".mp3")


async def tts_bytes(text, voice_id, use_cache=True):
    """Audio for text in voice_id. Cached on disk so phrases keep playing offline."""
    path = cache_path(voice_id, text)
    if use_cache and os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    if not ELEVEN_KEY:
        raise RuntimeError("no ELEVENLABS_API_KEY in .env")
    try:
        async with http.post(f"{ELEVEN}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64",
                             json={"text": text, "model_id": TTS_MODEL}, headers=eleven_headers(True),
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
            body = await r.read()
            if r.status != 200:
                raise RuntimeError(eleven_error(r.status, body.decode(errors="replace")))
    except asyncio.TimeoutError:
        raise RuntimeError("ElevenLabs timeout")
    except aiohttp.ClientError as e:
        raise RuntimeError(f"network: {e.__class__.__name__}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(body)
    os.replace(path + ".part", path)
    eleven_health.update(ok=True, err="")
    return body


async def tts(request):
    text = " ".join(request.query.get("text", "").split())[:160]
    if not text:
        return web.Response(status=400, text="text required\n")
    voice_id = request.query.get("v") or current_voice_id()
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", voice_id):
        return web.Response(status=400, text="bad voice id\n")
    try:
        audio = await tts_bytes(text, voice_id)
    except RuntimeError as e:
        eleven_health.update(ok=False, err=str(e))
        log(f"TTS fail: {e}")
        return web.Response(status=503, text=str(e))
    return web.Response(body=audio, content_type="audio/mpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


def warm_cache(texts, voice_id=None):
    """Fetch audio for these phrases in the background (3 at a time)."""
    if not ELEVEN_KEY:
        return
    voice_id = voice_id or current_voice_id()

    async def one(t):
        async with warm_sem:
            try:
                await tts_bytes(t, voice_id)
            except RuntimeError as e:
                eleven_health.update(ok=False, err=str(e))

    for t in {t for t in texts if t}:
        if not os.path.exists(cache_path(voice_id, t)):
            asyncio.ensure_future(one(t))


def all_phrases():
    texts = [g["text"] for g in grid_items()] + state["suggestions"]["phrases"] + offline_phrases()
    texts += BASIC_ANSWERS + [SOMETHING_ELSE, "Can you help me?", "Please come here", "I'm cold",
                              "Please wait", "Good morning", "I'm hungry", "I'm tired"]
    if state["question"]:
        texts += state["question"]["options"]
    seen, out = set(), []
    for t in texts:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def regenerate_all(voice_id):
    """Re-create every cached phrase in the new voice, reporting progress to the dashboard."""
    texts = all_phrases()
    regen.update(running=True, done=0, total=len(texts))
    await to_dashboards({"type": "VOICE_PROGRESS", **regen})
    sem = asyncio.Semaphore(3)

    async def one(t):
        async with sem:
            try:
                await tts_bytes(t, voice_id)
            except RuntimeError as e:
                log(f"regenerate {t!r}: {e}")
            regen["done"] += 1
            await to_dashboards({"type": "VOICE_PROGRESS", **regen})

    await asyncio.gather(*(one(t) for t in texts))
    regen["running"] = False
    await to_dashboards({"type": "VOICE_PROGRESS", **regen})
    await push_state()                          # talking screens switch to the new voice
    log(f"Voice cache regenerated: {regen['done']}/{regen['total']} phrases in {voice_id}")


# ----- voice banking endpoints (dashboard, local only)
async def eleven_get(path):
    async with http.get(f"{ELEVEN}{path}", headers=eleven_headers(), timeout=aiohttp.ClientTimeout(total=10)) as r:
        text = await r.text()
        if r.status != 200:
            raise RuntimeError(eleven_error(r.status, text))
        return json.loads(text)


async def voice_info(_request):
    out = {"voice": voice_public(), "regen": regen, "plan": None, "can_clone": None, "plan_err": ""}
    try:
        sub = await eleven_get("/v1/user/subscription")
        out["plan"] = sub.get("tier")
        out["can_clone"] = sub.get("can_use_instant_voice_cloning")
        out["credits"] = {"used": sub.get("character_count"), "limit": sub.get("character_limit")}
    except RuntimeError as e:
        out["plan_err"] = str(e)
    try:
        out["samples_seconds"] = sample_seconds()
    except OSError:
        out["samples_seconds"] = 0
    return web.json_response(out)


async def voice_library(_request):
    try:
        d = await eleven_get("/v1/voices")
    except RuntimeError as e:
        return web.json_response({"ok": False, "reason": str(e)})
    voices = [{"voice_id": v["voice_id"], "name": v["name"], "category": v.get("category"),
               "preview_url": v.get("preview_url"), "labels": v.get("labels") or {}}
              for v in d.get("voices", []) if v.get("category") in ("premade", "professional", "generated", "cloned")]
    return web.json_response({"ok": True, "voices": voices})


async def run(*cmd):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def sample_seconds():
    total = 0.0
    if os.path.isdir(VOICE_TMP):
        for fn in os.listdir(VOICE_TMP):
            if fn.endswith(".secs"):
                total += float(open(os.path.join(VOICE_TMP, fn)).read() or 0)
    return round(total, 1)


async def voice_upload(request):
    """Recorded or uploaded audio/video -> mono MP3 with silences trimmed (ffmpeg)."""
    import uuid
    os.makedirs(VOICE_TMP, exist_ok=True)
    reader = await request.multipart()
    consent, saved = False, []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "consent":
            consent = (await part.text()) == "yes"
        elif part.name == "file":
            if not consent:
                return web.json_response({"ok": False, "reason": "consent is required before uploading audio"}, status=400)
            raw = os.path.join(VOICE_TMP, uuid.uuid4().hex + ".raw")
            size = 0
            with open(raw, "wb") as f:
                while True:
                    chunk = await part.read_chunk(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 60 * 1024 * 1024:
                        f.close(); os.remove(raw)
                        return web.json_response({"ok": False, "reason": "file too large (60 MB max)"})
                    f.write(chunk)
            mp3 = raw[:-4] + ".mp3"
            code, _, err = await run("ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-vn", "-ac", "1", "-ar", "44100",
                                     "-af", "silenceremove=start_periods=1:start_threshold=-45dB:"
                                            "stop_periods=-1:stop_duration=0.6:stop_threshold=-45dB",
                                     "-b:a", "128k", mp3)
            os.remove(raw)
            if code != 0 or not os.path.exists(mp3):
                return web.json_response({"ok": False, "reason": f"couldn't read {part.filename or 'file'}: {err.strip()[-160:]}"})
            code, out, _ = await run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp3)
            secs = float(out.strip() or 0)
            if secs < 1:
                os.remove(mp3)
                continue
            open(mp3[:-4] + ".secs", "w").write(str(secs))
            saved.append({"name": part.filename or "recording", "seconds": round(secs, 1)})
    log(f"VOICE samples added: {saved} (total {sample_seconds()} s)")
    return web.json_response({"ok": True, "added": saved, "total_seconds": sample_seconds()})


def clear_samples():
    import shutil
    shutil.rmtree(VOICE_TMP, ignore_errors=True)


async def voice_clear(_request):
    clear_samples()
    return web.json_response({"ok": True, "total_seconds": 0})


async def voice_clone(request):
    d = await request.json()
    if d.get("consent") is not True:
        return web.json_response({"ok": False, "reason": "consent is required"}, status=400)
    files = sorted(f for f in os.listdir(VOICE_TMP) if f.endswith(".mp3")) if os.path.isdir(VOICE_TMP) else []
    if not files:
        return web.json_response({"ok": False, "reason": "no usable audio yet"})
    name = (d.get("name") or "My voice")[:40]
    t0 = time.time()
    form = aiohttp.FormData()
    form.add_field("name", name)
    form.add_field("remove_background_noise", "true")
    form.add_field("description", "Sirenize voice bank (consented)")
    handles = []
    for fn in files[:25]:
        fh = open(os.path.join(VOICE_TMP, fn), "rb")
        handles.append(fh)
        form.add_field("files", fh, filename=fn, content_type="audio/mpeg")
    try:
        async with http.post(f"{ELEVEN}/v1/voices/add", data=form, headers=eleven_headers(),
                             timeout=aiohttp.ClientTimeout(total=120)) as r:
            text = await r.text()
            if r.status != 200:
                reason = eleven_error(r.status, text)
                if "missing the permission" in reason:
                    perm = re.search(r"permission (\w+)", reason)
                    return web.json_response({"ok": False, "reason": reason, "fallback":
                        f"Turn on '{perm.group(1) if perm else 'Voices: Write'}' for this API key at elevenlabs.io "
                        "→ API Keys → Edit, then press Create my voice again. Your audio is still saved."})
                return web.json_response({"ok": False, "reason": reason,
                                          "fallback": "Cloning isn't available with this ElevenLabs plan or key. "
                                                      "Pick a voice that feels like you instead."})
            voice_id = json.loads(text)["voice_id"]
    finally:
        for fh in handles:
            fh.close()
    secs = round(time.time() - t0, 1)
    clear_samples()                              # never keep raw voice audio after cloning
    log(f"VOICE cloned {name!r} -> {voice_id} in {secs} s; raw samples deleted")
    return web.json_response({"ok": True, "voice_id": voice_id, "name": name, "seconds": secs, "source": "clone"})


async def voice_design(request):
    d = await request.json()
    desc = " ".join(str(d.get("description", "")).split())[:500]
    if len(desc) < 10:
        return web.json_response({"ok": False, "reason": "describe the voice in a few words (10+ characters)"})
    try:
        async with http.post(f"{ELEVEN}/v1/text-to-voice/design?output_format=mp3_44100_64",
                             json={"voice_description": desc, "auto_generate_text": True},
                             headers=eleven_headers(True), timeout=aiohttp.ClientTimeout(total=60)) as r:
            text = await r.text()
            if r.status != 200:
                return web.json_response({"ok": False, "reason": eleven_error(r.status, text)})
            out = json.loads(text)
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "reason": "ElevenLabs timeout"})
    previews = [{"generated_voice_id": p["generated_voice_id"], "audio": p["audio_base_64"],
                 "media_type": p.get("media_type", "audio/mpeg")} for p in out.get("previews", [])]
    return web.json_response({"ok": True, "previews": previews, "description": desc})


async def voice_design_save(request):
    d = await request.json()
    body = {"voice_name": (d.get("name") or "My voice")[:40], "voice_description": d.get("description", "")[:500],
            "generated_voice_id": d.get("generated_voice_id", "")}
    async with http.post(f"{ELEVEN}/v1/text-to-voice", json=body, headers=eleven_headers(True),
                         timeout=aiohttp.ClientTimeout(total=30)) as r:
        text = await r.text()
        if r.status != 200:
            return web.json_response({"ok": False, "reason": eleven_error(r.status, text)})
        return web.json_response({"ok": True, "voice_id": json.loads(text)["voice_id"], "name": body["voice_name"],
                                  "source": "designed"})


async def voice_preview(request):
    voice_id = request.query.get("v", "")
    i = int(request.query.get("i", "0")) % len(SAMPLE_PHRASES)
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", voice_id):
        return web.Response(status=400, text="bad voice id\n")
    try:
        audio = await tts_bytes(SAMPLE_PHRASES[i], voice_id)
    except RuntimeError as e:
        return web.Response(status=503, text=str(e))
    return web.Response(body=audio, content_type="audio/mpeg")


async def voice_approve(request):
    d = await request.json()
    voice_id = d.get("voice_id", "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", voice_id):
        return web.json_response({"ok": False, "reason": "bad voice id"})
    v = {"voice_id": voice_id, "name": (d.get("name") or "My voice")[:40], "source": d.get("source", "clone"),
         "created": datetime.now().isoformat(timespec="minutes")}
    with open(VOICE_FILE, "w") as f:
        json.dump(v, f, indent=2)
    log(f"VOICE approved: {v}")
    asyncio.ensure_future(regenerate_all(voice_id))
    return web.json_response({"ok": True, "voice": voice_public(), "total": len(all_phrases())})


async def voice_delete(_request):
    v = load_voice()
    if not v:
        return web.json_response({"ok": False, "reason": "no personal voice is set"})
    confirmation = "not a cloned voice: just switched back to the default voice"
    if v.get("source") in ("clone", "designed"):
        async with http.delete(f"{ELEVEN}/v1/voices/{v['voice_id']}", headers=eleven_headers(),
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            text = await r.text()
            if r.status != 200:
                return web.json_response({"ok": False, "reason": eleven_error(r.status, text)})
            confirmation = text
    os.remove(VOICE_FILE)
    import shutil
    shutil.rmtree(os.path.join(AUDIO_CACHE, v["voice_id"]), ignore_errors=True)
    log(f"VOICE deleted {v['voice_id']}: {confirmation}")
    warm_cache(all_phrases())
    await push_state()
    return web.json_response({"ok": True, "api_response": confirmation, "voice": voice_public()})


# ----- hearing on the talking screen
HEAR_EXTRA = "Can you say that again?"
HEAR_PROMPT = (
    "You help a person with ALS reply to someone who is talking to them right now. They cannot speak or use "
    "their hands; they pick a reply with head movements, and it is spoken in their own voice.\n"
    "What the other person just said (speech transcript, may be messy or partial): \"{heard}\"\n"
    "About the user: {profile}\n"
    "What the camera sees (may be empty): \"{scene}\"\n"
    "What the user said recently: {recent}\n"
    "Write exactly 3 different replies the user would most likely want to say back.\n"
    "Rules: first person, natural spoken English, 2-7 words each, warm and dignified. Make each reply specific to "
    "what was said (for 'Do you want to go outside?' write e.g. 'Yes, let's go outside', 'Not today, I'm tired', "
    "'Maybe after lunch' - never just 'Yes' or 'No' on their own). Cover different intents: agree, decline or "
    "postpone, and a useful follow-up. Never give medical advice or invent facts about people. If the transcript "
    "is unclear, give replies that still make sense.\n"
    "Return JSON: {{\"options\": [3 strings]}}."
)
HEAR_SCHEMA = {"type": "OBJECT", "properties": {"options": {"type": "ARRAY", "items": {"type": "STRING"}}},
               "required": ["options"]}


async def hear(request):
    """Audio from the ACTIVE talking screen's mic: someone spoke to the user.
    Transcribe it, ask Gemini for replies, and put them in the shared Suggested row."""
    global heard_until
    dev = request.headers.get("X-Device", "")
    if not active["id"] or dev != active["id"]:
        return web.json_response({"ok": False, "reason": "not the active device"})
    t0 = time.time()
    reader = await request.multipart()
    audio, mime = None, "audio/mp4"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            mime = part.headers.get("Content-Type", mime)
            audio = await part.read()
    if not audio or not ELEVEN_KEY:
        return web.json_response({"ok": False, "reason": "no audio" if not audio else "speech-to-text not set up"})
    ext = "mp4" if "mp4" in mime else "webm"
    form = aiohttp.FormData()
    form.add_field("model_id", STT_MODEL)
    form.add_field("file", audio, filename=f"speech.{ext}", content_type=mime)
    try:
        async with http.post(f"{ELEVEN}/v1/speech-to-text", data=form, headers=eleven_headers(),
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
            text = await r.text()
            if r.status != 200:
                err = eleven_error(r.status, text)
                eleven_health.update(ok=False, err=err)
                log(f"HEAR: speech-to-text failed: {err}")
                return web.json_response({"ok": False, "reason": "Didn't catch that"})
            transcript = " ".join(json.loads(text).get("text", "").split())
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        log(f"HEAR: speech-to-text {e.__class__.__name__}")
        return web.json_response({"ok": False, "reason": "Didn't catch that"})
    stt_ms = int((time.time() - t0) * 1000)
    words = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", transcript)
    if len(re.findall(r"[A-Za-z]{2,}", words)) < 2:            # noise, a cough, "(music)"...
        return web.json_response({"ok": False, "reason": "Didn't catch that", "stt_ms": stt_ms})
    transcript = transcript[:200]
    await add_convo(transcript, "heard")
    prompt = HEAR_PROMPT.format(heard=transcript.replace('"', "'"), profile=json.dumps(load_profile(), ensure_ascii=False),
                                scene=camera_state.get("scene", ""), recent=json.dumps(recent_said))
    try:
        data, gem_ms = await gemini_json([{"text": prompt}], HEAR_SCHEMA)
        options = clean_options(data.get("options"), "open")
        health["gemini"] = {"ok": True, "err": ""}
    except RuntimeError as e:
        health["gemini"] = {"ok": False, "err": str(e)}
        options, gem_ms = list(BASIC_ANSWERS), None
        log(f"HEAR: Gemini fallback ({e})")
    options = [o for o in options if _norm(o) != _norm(HEAR_EXTRA)][:3] + [HEAR_EXTRA]   # no duplicate
    heard_until = time.time() + HEARD_HOLD_S
    offer_suggestions(options, "heard", transcript)
    total = int((time.time() - t0) * 1000)
    log(f"HEAR {total} ms (stt {stt_ms} ms, gemini {gem_ms} ms): {transcript!r} -> {options}")
    return web.json_response({"ok": True, "transcript": transcript, "options": options,
                              "stt_ms": stt_ms, "gemini_ms": gem_ms, "ms": total})


# ----- listen mode
async def listen_toggle(request):
    d = await request.json()
    listen_state["on"] = bool(d.get("on"))
    await push_state()
    return web.json_response({"ok": True, **listen_state})


async def listen(request):
    """Audio of someone talking TO the user -> transcript -> reply options on the talking screen."""
    t0 = time.time()
    reader = await request.multipart()
    audio, browser_text, mime = None, "", "audio/webm"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            mime = part.headers.get("Content-Type", mime)
            audio = await part.read()
        elif part.name == "browser_text":
            browser_text = " ".join((await part.text()).split())[:300]
    transcript, engine, err = "", "", ""
    if audio and ELEVEN_KEY:
        form = aiohttp.FormData()
        form.add_field("model_id", STT_MODEL)
        form.add_field("file", audio, filename="speech.webm", content_type=mime)
        try:
            async with http.post(f"{ELEVEN}/v1/speech-to-text", data=form, headers=eleven_headers(),
                                 timeout=aiohttp.ClientTimeout(total=6)) as r:
                text = await r.text()
                if r.status == 200:
                    transcript = " ".join(json.loads(text).get("text", "").split())
                    engine = "ElevenLabs"
                    eleven_health.update(ok=True, err="")
                else:
                    err = eleven_error(r.status, text)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            err = f"ElevenLabs STT: {e.__class__.__name__}"
    stt_ms = int((time.time() - t0) * 1000)
    if not transcript and browser_text:
        transcript, engine = browser_text, "browser"
    # drop noise-only transcripts like "(music)" or "..."
    if not re.search(r"[A-Za-z]{2,}", re.sub(r"\([^)]*\)|\[[^\]]*\]", "", transcript)):
        log(f"LISTEN: didn't catch that ({err or 'empty transcript'})")
        return web.json_response({"ok": False, "reason": "Didn't catch that", "detail": err, "stt_ms": stt_ms})
    q = await ask_core(transcript[:200], mode="listen",
                       extra={"stt_ms": stt_ms, "engine": engine, "server_received_ts": int(t0 * 1000)})
    log(f"LISTEN [{engine}] stt {stt_ms} ms: {transcript!r}")
    return web.json_response({"ok": True, "transcript": transcript, "engine": engine, "stt_ms": stt_ms,
                              "question": q, "total_ms": int((time.time() - t0) * 1000)})


# ---------------------------------------------------------------- data (dashboard)
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def data_summary():
    rows = read_csv(CONVO_CSV)
    today = datetime.now().date().isoformat()
    by_how, phrases = {}, {}
    urgent_today = 0
    for r in rows:
        by_how[r["how"]] = by_how.get(r["how"], 0) + 1
        phrases[r["phrase"]] = phrases.get(r["phrase"], 0) + 1
        if r.get("urgent") == "yes" and r["time"].startswith(today):
            urgent_today += 1
    trials = read_csv(TRIALS)
    speed = {}
    for t in trials:
        a = speed.setdefault(t["source"], {"n": 0, "moves": 0, "ms": 0})
        a["n"] += 1
        a["moves"] += int(t["moves_since_last_select"])
        a["ms"] += int(t["ms_since_first_move"])
    return {
        "total": len(rows),
        "today": sum(1 for r in rows if r["time"].startswith(today)),
        "urgent_today": urgent_today,
        "by_how": by_how,
        "top_phrases": sorted(phrases.items(), key=lambda kv: -kv[1])[:8],
        "speed": {k: {"n": v["n"], "avg_moves": round(v["moves"] / v["n"], 1),
                      "avg_seconds": round(v["ms"] / v["n"] / 1000, 1)} for k, v in speed.items()},
        "gestures": {g: counts.get(g, 0) for g in GESTURES},
    }


async def summary(_request):
    return web.json_response(data_summary())


def csv_download(path, name):
    async def handler(_request):
        if not os.path.exists(path):
            return web.Response(text="no data yet\n", content_type="text/plain")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                               "Content-Type": "text/csv"})
    return handler


async def alert_clear(_request):
    """Dashboard dismisses the urgent banner. Nothing is sent to the talking screen."""
    global alert
    alert = None
    await to_dashboards({"type": "ALERT_CLEAR"})
    return web.json_response({"ok": True})


async def dashboard_home(_request):
    return web.FileResponse(os.path.join(PUBLIC, "dashboard.html"), headers=NO_CACHE)


# ---------------------------------------------------------------- app
async def on_startup(_app):
    global loop, http
    loop = asyncio.get_running_loop()
    http = aiohttp.ClientSession()
    _app["internet_task"] = asyncio.ensure_future(internet_loop())
    _app["scheduler_task"] = asyncio.ensure_future(scheduler())
    global warm_sem
    warm_sem = asyncio.Semaphore(3)
    go_offline()
    state["suggestions"], _ = {"phrases": offline_phrases(), "source": "offline", "scene": ""}, None
    warm_cache(all_phrases())
    if SOURCE == "bridge":
        _app["watchdog_task"] = asyncio.ensure_future(heartbeat_watchdog())


async def on_cleanup(_app):
    _app["internet_task"].cancel()
    _app["scheduler_task"].cancel()
    if "watchdog_task" in _app:
        _app["watchdog_task"].cancel()
    await http.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--http-port", type=int, default=8080, help="talking screen (phone/iPad; the tunnel points here)")
    ap.add_argument("--dashboard-port", type=int, default=8088, help="caregiver dashboard (local network only)")
    ap.add_argument("--source", choices=["serial", "bridge"], default="serial",
                    help="serial: Mega over USB (default). bridge: UNO Q sensor app posts to /event and /heartbeat")
    args = ap.parse_args()
    global SOURCE, TALK_PORT
    SOURCE = args.source
    TALK_PORT = args.http_port

    # Talking screen: everything the user's device needs, nothing else.
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/go", go)
    app.router.add_get("/tts", tts)
    app.router.add_post("/predict", predict)
    app.router.add_post("/hear", hear)
    app.router.add_post("/log", log_trial)
    app.router.add_get("/manifest.webmanifest", static_file("manifest.webmanifest", "application/manifest+json"))
    app.router.add_get("/icon-180.png", static_file("icon-180.png", "image/png"))
    app.router.add_get("/icon-512.png", static_file("icon-512.png", "image/png"))
    app.router.add_get("/fake/{gesture}", local_only(fake))
    app.router.add_get("/calibrate", local_only(calibrate))
    app.router.add_post("/event/{gesture}", local_only(bridge_event))
    app.router.add_post("/heartbeat", local_only(heartbeat))
    app.router.add_post("/sensor_status", local_only(sensor_status))

    # Dashboard: separate port, never behind the tunnel. Voice banking + data only.
    # It only reads data; nothing it does is spoken or shown on the talking screen.
    dash = web.Application(client_max_size=64 * 1024 * 1024)
    dash.router.add_get("/", local_only(dashboard_home))
    dash.router.add_get("/ws", dashboard_ws)
    dash.router.add_get("/summary", local_only(summary))
    dash.router.add_get("/stats", local_only(stats))
    dash.router.add_get("/export/conversation.csv", local_only(csv_download(CONVO_CSV, "sirenize-conversation.csv")))
    dash.router.add_get("/export/selections.csv", local_only(csv_download(TRIALS, "sirenize-selections.csv")))
    dash.router.add_post("/alert/clear", local_only(alert_clear))
    dash.router.add_get("/links", local_only(links))
    dash.router.add_get("/voice", local_only(voice_info))
    dash.router.add_get("/voice/library", local_only(voice_library))
    dash.router.add_post("/voice/upload", local_only(voice_upload))
    dash.router.add_post("/voice/clear", local_only(voice_clear))
    dash.router.add_post("/voice/clone", local_only(voice_clone))
    dash.router.add_post("/voice/design", local_only(voice_design))
    dash.router.add_post("/voice/design/save", local_only(voice_design_save))
    dash.router.add_get("/voice/preview", local_only(voice_preview))
    dash.router.add_post("/voice/approve", local_only(voice_approve))
    dash.router.add_post("/voice/delete", local_only(voice_delete))

    if SOURCE == "serial":
        threading.Thread(target=serial_thread, args=(args.port,), daemon=True).start()
    else:
        log("Source: bridge (UNO Q sensor app); no serial port")
    log(f"Talking screen on http://0.0.0.0:{args.http_port}")
    log(f"Dashboard on http://0.0.0.0:{args.dashboard_port} (local network only, not on the tunnel)")
    log(f"Gemini: model={GEMINI_MODEL} thinking={GEMINI_THINKING} key={'set' if GEMINI_KEY else 'MISSING'}")

    async def serve():
        runners = []
        for a, port in ((app, args.http_port), (dash, args.dashboard_port)):
            r = web.AppRunner(a, access_log=None, shutdown_timeout=1)
            await r.setup()
            await web.TCPSite(r, "0.0.0.0", port).start()
            runners.append(r)
        try:
            await asyncio.Event().wait()
        finally:
            for r in runners:
                await r.cleanup()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
