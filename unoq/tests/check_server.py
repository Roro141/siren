#!/usr/bin/env python3
"""Automated checks for the Sirenize server (UNO Q version).

Starts server.py on spare ports (bridge mode, Gemini paused so no quota is used) and checks:
  - the talking screen and the dashboard are separate (no dashboard/voice routes on the public port)
  - two screens stay in sync; tilts move the shared highlight; a nod selects and is logged
  - only the active device may send camera frames or heard audio
  - the active device is released 10 s after it disconnects
  - data summary and CSV exports work

    python3 tests/check_server.py            (needs aiohttp; run from the unoq/ folder)
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import aiohttp

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def next_state(ws, timeout=3, cond=lambda d: True):
    end = time.time() + timeout
    while time.time() < end:
        msg = await ws.receive(timeout=end - time.time())
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        d = json.loads(msg.data)
        if d.get("type") == "STATE" and cond(d):
            return d
    return None


async def main(talk, dash):
    T, D = f"http://127.0.0.1:{talk}", f"http://127.0.0.1:{dash}"
    async with aiohttp.ClientSession() as http:
        async def code(url, method="GET", **kw):
            async with http.request(method, url, allow_redirects=False, **kw) as r:
                await r.read()
                return r.status

        # --- separation of the two servers
        check("talking screen page", await code(T + "/") == 200)
        check("phone bookmark /go answers", await code(T + "/go") in (200, 302))
        check("home-screen manifest + icon", await code(T + "/manifest.webmanifest") == 200 and await code(T + "/icon-180.png") == 200)
        hidden = {p: await code(T + p) for p in ("/dashboard", "/voice", "/summary", "/export/conversation.csv", "/links")}
        check("no dashboard or voice routes on the public port", all(c == 404 for c in hidden.values()), str(hidden))
        check("dashboard page on its own port", await code(D + "/") == 200)
        for p in ("/summary", "/voice", "/links", "/export/conversation.csv", "/export/selections.csv"):
            check(f"dashboard {p}", await code(D + p) == 200)
        check("voice text-to-speech answers (200, or 503 without an ElevenLabs key)", await code(T + "/tts?text=Yes") in (200, 503))

        # --- two screens
        a = await http.ws_connect(T + "/ws?device=test-iphone&name=iPhone")
        b = await http.ws_connect(T + "/ws?device=test-ipad&name=iPad")
        sa, sb = await next_state(a), await next_state(b)
        check("both screens get the full state on connect", bool(sa and sb))
        check("same grid order on both", sa["grid"] == sb["grid"], f"{len(sa['grid'])} tiles")
        await a.send_str(json.dumps({"type": "CLAIM", "force": True, "tz": 0}))
        s = await next_state(b, cond=lambda d: d["active"]["id"] == "test-iphone")
        check("Talk here on the iPhone -> iPad sees it active", bool(s), s and s["active"]["name"])

        for g in ("RIGHT", "RIGHT", "RIGHT"):
            await code(T + f"/fake/{g}")
        s = await next_state(b, cond=lambda d: d["highlight"] == 2)
        check("three tilts move the shared highlight to tile 3 on every screen", bool(s))
        await code(T + "/fake/LEFT")
        s = await next_state(a, cond=lambda d: d["highlight"] == 1)
        check("LEFT moves it back", bool(s))
        await code(T + "/fake/NOD")
        s = await next_state(b, cond=lambda d: d["spoken"] is not None)
        check("NOD selects: both screens show the spoken phrase", bool(s), s and s["spoken"]["text"])
        await code(T + "/fake/HOME")
        s = await next_state(a, cond=lambda d: d["highlight"] == -1)
        check("HOME resets the highlight", bool(s))

        # --- only the active device may use the camera / microphone
        async with http.post(T + "/predict", json={"image_b64": "x"}, headers={"X-Device": "test-ipad"}) as r:
            d = await r.json()
        check("camera frame from a mirror device is refused", d.get("reason") == "not the active device")
        async with http.post(T + "/predict", json={"image_b64": "x"}, headers={"X-Device": "test-iphone"}) as r:
            d = await r.json()
        check("camera frame while Gemini is paused uses no quota", d.get("reason") == "Gemini paused", d.get("reason"))
        fd = aiohttp.FormData()
        fd.add_field("file", b"\0" * 10, filename="x.mp4", content_type="audio/mp4")
        async with http.post(T + "/hear", data=fd, headers={"X-Device": "test-ipad"}) as r:
            d = await r.json()
        check("heard audio from a mirror device is refused", d.get("reason") == "not the active device")

        # --- takeover + release
        t0 = time.time()
        await b.send_str(json.dumps({"type": "CLAIM", "force": True}))
        s = await next_state(a, cond=lambda d: d["active"]["id"] == "test-ipad")
        check("iPad takes over in under 1 s", bool(s) and time.time() - t0 < 1, f"{int((time.time() - t0) * 1000)} ms")
        await b.close()
        s = await next_state(a, timeout=14, cond=lambda d: d["active"]["id"] is None)
        check("active device released ~10 s after it disconnects", bool(s))
        await a.close()

        # --- data
        async with http.get(D + "/summary") as r:
            summ = await r.json()
        check("data summary counts the selection", summ.get("total", 0) >= 1, f"{summ.get('total')} phrases logged")


def run():
    talk, dash = free_port(), free_port()
    env = dict(os.environ, SIRENIZE_GEMINI_PAUSED="1")
    proc = subprocess.Popen([sys.executable, "server.py", "--source", "bridge", "--http-port", str(talk),
                             "--dashboard-port", str(dash)], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", dash), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        asyncio.run(main(talk, dash))
    finally:
        proc.terminate()
        proc.wait(5)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    run()
