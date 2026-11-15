# Sirenize sensor app (Linux side): Bridge -> local HTTP -> server.py
#
# The sketch calls Bridge.notify("gesture"/"heartbeat"/"status", ...).
# Each one is forwarded to server.py (running as its own service on the board)
# with a local HTTP POST. HTTP happens on a worker thread so Bridge callbacks
# never block, and Bridge.call() is only made from the user loop (never from
# inside a Bridge callback, which can deadlock).

import json
import queue
import threading
import time
import urllib.request

from arduino.app_utils import *

logger = Logger("sirenize-sensor")

# The app runs in a container; try the usual ways to reach the host's server.
CANDIDATES = ["http://127.0.0.1:8080", "http://host.docker.internal:8080", "http://172.17.0.1:8080"]
server = None
outbox = queue.Queue(maxsize=200)
calibrate_wanted = threading.Event()


def post(path, body):
    global server
    hosts = [server] if server else CANDIDATES
    for base in hosts:
        try:
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=2) as r:
                if server != base:
                    logger.info(f"server reachable at {base}")
                server = base
                return json.loads(r.read() or b"{}")
        except Exception as e:
            if server == base:
                logger.warning(f"POST {path} to {base} failed: {e}")
                server = None
    return None


def worker():
    while True:
        path, body = outbox.get()
        reply = post(path, body)
        if reply and reply.get("calibrate"):
            calibrate_wanted.set()


threading.Thread(target=worker, daemon=True).start()


def send(path, body):
    try:
        outbox.put_nowait((path, body))
    except queue.Full:
        logger.warning("outbox full, dropping " + path)


def gesture(name):
    name = str(name)
    logger.info(f"gesture {name}")
    send(f"/event/{name}", {"source": "sensor"})


def heartbeat(ok):
    send("/heartbeat", {"ok": bool(ok)})


def status(text):
    text = str(text)
    logger.info(f"sensor: {text}")
    send("/sensor_status", {"text": text})


Bridge.provide("gesture", gesture)
Bridge.provide("heartbeat", heartbeat)
Bridge.provide("status", status)


def loop():
    # Re-zero requested by the server (via the heartbeat reply).
    if calibrate_wanted.wait(timeout=0.5):
        calibrate_wanted.clear()
        try:
            Bridge.call("calibrate")
            logger.info("calibrate requested on the sensor")
        except Exception as e:
            logger.warning(f"calibrate call failed: {e}")


logger.info("Sirenize sensor app starting")
App.run(user_loop=loop)
