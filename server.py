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
from datetime import datetime

import aiohttp
import serial
from aiohttp import WSMsgType, web

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
TRIALS = os.path.join(HERE, "trials.csv")
GESTURES = ("LEFT", "RIGHT", "NOD")
FAKE_TYPES = GESTURES + ("HOME",)
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*")
FIXED_PHRASES = ["I need help", "I need water", "Bathroom", "I'm in pain", "Yes", "No", "Thank you", "Never mind"]

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

clients = set()
counts = {g: 0 for g in FAKE_TYPES}
loop = None
ser = None
ser_lock = threading.Lock()
mega_connected = False
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
    msg = json.dumps({"type": gesture, "server_ms": int(time.time() * 1000)})
    log(f"EVENT {gesture:<5} #{counts[gesture]:<3} ({source}) -> {len(clients)} client(s)")
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            clients.discard(ws)


async def send_status():
    msg = json.dumps({"mega": mega_connected})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            clients.discard(ws)


def set_mega(connected):
    global mega_connected
    mega_connected = connected
    if loop:
        asyncio.run_coroutine_threadsafe(send_status(), loop)


def serial_thread(forced_port):
    global ser
    announced_missing = False
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
                    if line.startswith("EVENT "):
                        parts = line.split()
                        if len(parts) > 1 and parts[1] in GESTURES and loop:
                            asyncio.run_coroutine_threadsafe(broadcast(parts[1], "serial"), loop)
                    elif line.startswith((">>", "!!", "WHO_AM_I", "SOMEBODY")):
                        log(f"Mega: {line}")
        except (serial.SerialException, OSError) as e:
            log(f"Serial: disconnected ({e.__class__.__name__}), retrying every 2 s...")
        finally:
            with ser_lock:
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                ser = None
            set_mega(False)
        time.sleep(2)


async def index(_request):
    return web.FileResponse(os.path.join(PUBLIC, "index.html"))


async def fake(request):
    gesture = request.match_info["gesture"].upper()
    if gesture not in FAKE_TYPES:
        raise web.HTTPNotFound(text="use LEFT, RIGHT, NOD or HOME\n")
    await broadcast(gesture, "fake")
    return web.Response(text=f"sent fake {gesture}\n")


async def calibrate(_request):
    with ser_lock:
        if not ser:
            return web.Response(status=503, text="Mega not connected\n")
        ser.write(b"c\n")
    log("Sent calibrate to Mega (hold still)")
    return web.Response(text="calibrating\n")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    clients.add(ws)
    log(f"Page connected ({len(clients)} client(s))")
    await ws.send_str(json.dumps({"mega": mega_connected}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if "ping" in data:
                    await ws.send_str(json.dumps({"pong": data["ping"]}))
                elif "rtt" in data:
                    log(f"Page round-trip: {data['rtt']} ms")
    finally:
        clients.discard(ws)
        log(f"Page disconnected ({len(clients)} client(s))")
    return ws


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

    def fail(reason, status=None):
        ms = int((time.time() - t0) * 1000)
        log(f"PREDICT fail {ms} ms: {reason}")
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


async def on_cleanup(_app):
    await http.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--http-port", type=int, default=8080)
    args = ap.parse_args()

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

    threading.Thread(target=serial_thread, args=(args.port,), daemon=True).start()
    log(f"Server on http://0.0.0.0:{args.http_port}")
    log(f"Gemini: model={GEMINI_MODEL} thinking={GEMINI_THINKING} key={'set' if GEMINI_KEY else 'MISSING'}")
    web.run_app(app, host="0.0.0.0", port=args.http_port, print=None, shutdown_timeout=1)


if __name__ == "__main__":
    main()
