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
        "elevenlabs": {"ok": None, "err": "not set up yet (using the phone's built-in voice)"},
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
            "grid": grid_items(), "active": dict(active), "mega": headband_ok()}


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
    cur = state["suggestions"]["phrases"]
    keep = [p if any(q.lower() == p.lower() for q in incoming) else None for p in cur]
    fresh = [q for q in incoming if not any(p and p.lower() == q.lower() for p in keep)]
    merged = [(p or (fresh.pop(0) if fresh else "")) for p in keep] + fresh
    merged = merged[:4]
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
            state["spoken"] = {"text": q["question"], "kind": "question", "asker": q["asker"], "seq": spoken_seq}
            asyncio.ensure_future(expire_question(q["id"]))
            await push_state()
        if pending_sugg and idle >= 3.0:
            state["suggestions"], pending_sugg = pending_sugg, None
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


def urgent_list():
    lst = load_profile().get("urgent_phrases")
    return [u for u in lst if isinstance(u, str)] if isinstance(lst, list) and lst else DEFAULT_URGENT


def notify_watch(text):
    # TODO(watch): push to the caregiver's Apple Watch. For now this only logs.
    log(f"NOTIFY_WATCH (stub): {text!r}")


async def add_convo(text, source, **extra):
    entry = {"ts": int(time.time() * 1000), "text": text, "source": source, **extra}
    convo.append(entry)
    del convo[:-300]
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
        "type": "INIT", "name": name, "caregiver": caregiver,
        "conversation": convo[-200:], "questions": list(questions.values())[-20:],
        "camera": camera_state, "alert": alert, "phrases": load_custom_phrases(),
        "health": health_snapshot(),
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
        if not str(e).startswith(("Gemini HTTP 503", "Gemini HTTP 429")) or left < 1.5:
            raise
        log(f"Gemini busy ({str(e)[:40]}), retrying once with {left:.1f} s left")
        data, _ = await _gemini_json_once(parts, schema, left)
        return data, int((time.time() - t0) * 1000)


async def _gemini_json_once(parts, schema, timeout_s):
    if not GEMINI_KEY:
        raise RuntimeError("no GEMINI_API_KEY in .env")
    t0 = time.time()
    payload = {"contents": [{"parts": parts}], "generationConfig": {
        "responseMimeType": "application/json", "responseSchema": schema,
        "thinkingConfig": {"thinkingLevel": GEMINI_THINKING}}}
    try:
        async with http.post(GEMINI_URL.format(model=GEMINI_MODEL), json=payload,
                             headers={"x-goog-api-key": GEMINI_KEY},
                             timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            text = await r.text()
            if r.status != 200:
                try:
                    msg = json.loads(text)["error"]["message"]
                except Exception:
                    msg = text[:200]
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
    global q_counter
    try:
        question = " ".join(str((await request.json())["question"]).split())[:200]
    except Exception:
        return web.json_response({"ok": False, "reason": "need JSON with question"}, status=400)
    if not question:
        return web.json_response({"ok": False, "reason": "empty question"}, status=400)
    t0 = time.time()
    q_counter += 1
    qid = f"q{int(t0)}-{q_counter}"
    name, caregiver = names()
    await add_convo(question, "question", qid=qid, asker=caregiver or "Caregiver")
    prompt = ASK_PROMPT.format(question=question.replace('"', "'"),
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
    # TODO(/tts): prefetch audio for all 4 options in parallel once /tts exists.
    q = {"id": qid, "question": question, "kind": kind, "options": options, "fallback": fallback,
         "reason": reason, "gemini_ms": gem_ms, "sent_ts": int(time.time() * 1000), "shown_ms": None,
         "answer": None, "answer_ms": None, "status": "waiting"}
    questions[qid] = q
    while len(questions) > 50:
        questions.pop(next(iter(questions)))
    global pending_question
    pending_question = {"id": qid, "question": question, "options": options, "asker": caregiver or ""}
    await to_dashboards({"type": "QUESTION_UPDATE", "question": q})
    await to_dashboards({"type": "HEALTH", **health_snapshot()})
    log(f"ASK {qid} ({int((time.time()-t0)*1000)} ms, gemini {gem_ms} ms) {question!r} -> {options}")
    return web.json_response({"ok": True, "question": q, "phones": len(clients)})


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
    port = request.url.port or 8080
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
        async with http.post(
            GEMINI_URL.format(model=GEMINI_MODEL),
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
    if phrases and conf >= 0.4:
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


# ---------------------------------------------------------------- app
async def on_startup(_app):
    global loop, http
    loop = asyncio.get_running_loop()
    http = aiohttp.ClientSession()
    _app["internet_task"] = asyncio.ensure_future(internet_loop())
    _app["scheduler_task"] = asyncio.ensure_future(scheduler())
    go_offline()
    state["suggestions"], _ = {"phrases": offline_phrases(), "source": "offline", "scene": ""}, None
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
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--source", choices=["serial", "bridge"], default="serial",
                    help="serial: Mega over USB (default). bridge: UNO Q sensor app posts to /event and /heartbeat")
    args = ap.parse_args()
    global SOURCE
    SOURCE = args.source

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/fake/{gesture}", fake)
    app.router.add_get("/calibrate", calibrate)
    app.router.add_post("/predict", predict)
    app.router.add_post("/log", log_trial)
    app.router.add_get("/stats", stats)
    app.router.add_get("/dashboard", local_only(dashboard_page))
    app.router.add_post("/ask", local_only(ask))
    app.router.add_get("/phrases", get_phrases)
    app.router.add_post("/phrases", local_only(add_phrase))
    app.router.add_post("/ack", local_only(ack))
    app.router.add_get("/go", go)
    app.router.add_get("/links", local_only(links))
    app.router.add_get("/manifest.webmanifest", static_file("manifest.webmanifest", "application/manifest+json"))
    app.router.add_get("/icon-180.png", static_file("icon-180.png", "image/png"))
    app.router.add_get("/icon-512.png", static_file("icon-512.png", "image/png"))
    app.router.add_post("/event/{gesture}", local_only(bridge_event))
    app.router.add_post("/heartbeat", local_only(heartbeat))
    app.router.add_post("/sensor_status", local_only(sensor_status))

    if SOURCE == "serial":
        threading.Thread(target=serial_thread, args=(args.port,), daemon=True).start()
    else:
        log("Source: bridge (UNO Q sensor app); no serial port")
    log(f"Server on http://0.0.0.0:{args.http_port}")
    log(f"Gemini: model={GEMINI_MODEL} thinking={GEMINI_THINKING} key={'set' if GEMINI_KEY else 'MISSING'}")
    log(f"Dashboard: http://localhost:{args.http_port}/dashboard (local network only)")
    web.run_app(app, host="0.0.0.0", port=args.http_port, print=None, shutdown_timeout=1)


if __name__ == "__main__":
    main()
