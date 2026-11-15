#!/usr/bin/env python3
"""SOMEBODY relay: Mega serial EVENT lines -> WebSocket -> phone page.

  python3 server.py [--port /dev/cu.usbmodemXXXX] [--http-port 8080]

Endpoints:
  /            public/index.html
  /ws          WebSocket: broadcasts {"type": "LEFT"|"RIGHT"|"NOD", "server_ms": ...}
               answers {"ping": n} with {"pong": n}
  /fake/<T>    broadcast a fake LEFT / RIGHT / NOD
  /calibrate   send "c" to the Mega (re-zero; hold head still and neutral)
"""
import argparse
import asyncio
import glob
import json
import os
import threading
import time
from datetime import datetime

import serial
from aiohttp import WSMsgType, web

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
GESTURES = ("LEFT", "RIGHT", "NOD")
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*")

clients = set()
counts = {g: 0 for g in GESTURES}
loop = None
ser = None
ser_lock = threading.Lock()
mega_connected = False


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


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
                        if len(parts) > 1 and parts[1] in GESTURES:
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
    if gesture not in GESTURES:
        raise web.HTTPNotFound(text="use LEFT, RIGHT or NOD")
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


async def on_startup(_app):
    global loop
    loop = asyncio.get_running_loop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--http-port", type=int, default=8080)
    args = ap.parse_args()

    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/fake/{gesture}", fake)
    app.router.add_get("/calibrate", calibrate)

    threading.Thread(target=serial_thread, args=(args.port,), daemon=True).start()
    log(f"Server on http://0.0.0.0:{args.http_port}")
    web.run_app(app, host="0.0.0.0", port=args.http_port, print=None, shutdown_timeout=1)


if __name__ == "__main__":
    main()
