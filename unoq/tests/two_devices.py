"""Browser test: a simulated iPhone and iPad (headless Chrome, DevTools protocol on port 9223).

Start Chrome with --headless=new --remote-debugging-port=9223, start server.py on 8090, then run this.
"""

import asyncio, base64, json, sys, time
import aiohttp

import os
BASE = os.environ.get("SIRENIZE_URL", "http://localhost:8090")
OUT = os.environ.get("SIRENIZE_SHOTS", "/tmp/sirenize")
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
IPAD_UA = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


class Page:
    def __init__(self, http, browser_ws, name):
        self.http, self.bws, self.name, self.n, self.errors = http, browser_ws, name, 0, []

    async def open(self, ua, w, h):
        ctx = await self.browser("Target.createBrowserContext")
        tgt = await self.browser("Target.createTarget", url="about:blank", browserContextId=ctx["browserContextId"])
        self.target = tgt["targetId"]
        targets = await (await self.http.get("http://127.0.0.1:9223/json")).json()
        url = next(t["webSocketDebuggerUrl"] for t in targets if t["id"] == self.target)
        self.ws = await self.http.ws_connect(url, max_msg_size=0)
        await self.cdp("Runtime.enable")
        await self.cdp("Page.enable")
        await self.cdp("Emulation.setUserAgentOverride", userAgent=ua)
        await self.cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=2, mobile=True)
        await self.cdp("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        # Record speech instead of playing it.
        await self.cdp("Page.addScriptToEvaluateOnNewDocument", source="""
          window.__spoken = [];
          const _speak = speechSynthesis.speak.bind(speechSynthesis);
          speechSynthesis.speak = (u) => { if (u.text) window.__spoken.push(u.text); };
          const _play = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function () {
            if (this.src && !this.src.startsWith('data:')) window.__spoken.push('audio:' + this.src.slice(0, 40));
            return Promise.resolve();
          };
        """)
        await self.cdp("Page.navigate", url=BASE + "/?t=" + str(time.time()))
        await asyncio.sleep(1.5)

    async def browser(self, method, **params):
        self.n += 1
        mid = 100000 + self.n
        await self.bws.send_json({"id": mid, "method": method, "params": params})
        while True:
            m = await self.bws.receive_json()
            if m.get("id") == mid:
                return m.get("result", m)

    async def cdp(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send_json({"id": mid, "method": method, "params": params})
        while True:
            m = await self.ws.receive_json()
            if m.get("method") == "Runtime.exceptionThrown":
                self.errors.append(m["params"]["exceptionDetails"].get("exception", {}).get("description", "?"))
            if m.get("id") == mid:
                return m.get("result", m)

    async def ev(self, js):
        r = await self.cdp("Runtime.evaluate", expression=js, returnByValue=True, userGesture=True)
        return r.get("result", {}).get("value")

    async def st(self):
        return json.loads(await self.ev("""JSON.stringify({
          active: isActive(), activeName: S && S.active && S.active.name, hl: S && S.highlight,
          hiText: (document.querySelector('.tile.hi')||{}).textContent || null,
          spoken: window.__spoken.slice(), bar: document.getElementById('deviceText').textContent,
          barShown: document.getElementById('deviceBar').classList.contains('show'),
          startShown: getComputedStyle(document.getElementById('start')).display !== 'none',
          cols: getComputedStyle(document.getElementById('grid')).gridTemplateColumns.split(' ').length,
          grid: [...document.querySelectorAll('#grid .tile')].map(t => t.textContent),
          sugg: [...document.querySelectorAll('#sugg .tile')].map(t => t.textContent),
          spokenText: document.getElementById('spoken').textContent })"""))

    async def shot(self, name):
        r = await self.cdp("Page.captureScreenshot", format="png")
        open(f"{OUT}_{name}.png", "wb").write(base64.b64decode(r["data"]))


async def fake(http, g):
    async with http.get(f"{BASE}/fake/{g}") as r:
        await r.text()


async def main():
    async with aiohttp.ClientSession() as http:
        ver = await (await http.get("http://127.0.0.1:9223/json/version")).json()
        bws = await http.ws_connect(ver["webSocketDebuggerUrl"], max_msg_size=0)
        phone, ipad = Page(http, bws, "iPhone"), Page(http, bws, "iPad")
        await phone.open(IPHONE_UA, 390, 760)
        await ipad.open(IPAD_UA, 1024, 1366)
        await asyncio.sleep(1)

        a, b = await phone.st(), await ipad.st()
        print("after load:", "phone active" if a["active"] else "phone mirror", "|", "ipad active" if b["active"] else "ipad mirror", "| ipad bar:", b["bar"])
        check("responsive: iPhone 3 cols / iPad 4 cols", a["cols"] == 3 and b["cols"] == 4, f"{a['cols']} / {b['cols']}")
        check("same tile order on both", a["grid"] == b["grid"] and a["sugg"] == b["sugg"], str(a["grid"][:3]))

        # iPhone taps Talk here
        await phone.ev("document.getElementById('talkHere').click(); document.getElementById('startBtn').click()")
        await asyncio.sleep(0.6)
        a, b = await phone.st(), await ipad.st()
        check("iPhone becomes active, iPad mirrors with banner", a["active"] and not b["active"] and "iPhone" in b["bar"], b["bar"])

        # Test 1: tilts move both in sync
        for g in ("RIGHT", "RIGHT", "RIGHT"):
            await fake(http, g); await asyncio.sleep(0.25)
        await asyncio.sleep(0.3)
        a, b = await phone.st(), await ipad.st()
        check("1. tilts move both in sync", a["hl"] == b["hl"] == 2 and a["hiText"] == b["hiText"], f"hl {a['hl']}/{b['hl']} '{a['hiText']}'")

        # Test 2: only the active device speaks
        await fake(http, "NOD"); await asyncio.sleep(0.6)
        a, b = await phone.st(), await ipad.st()
        check("2. only active device speaks on NOD", len(a["spoken"]) == 1 and len(b["spoken"]) == 0,
              f"iPhone said {a['spoken']}, iPad said {b['spoken']}")
        check("   both show the spoken phrase", a["spokenText"] == b["spokenText"] != "Nothing yet", a["spokenText"])
        await ipad.shot("ipad_mirror")
        await phone.shot("iphone_active")

        # Test 3: Talk here on the iPad takes over fast, same highlight
        await fake(http, "RIGHT"); await asyncio.sleep(0.3)
        before = (await ipad.st())["hl"]
        t0 = time.time()
        await ipad.ev("document.getElementById('talkHere').click()")
        took = None
        for _ in range(40):
            b = await ipad.st()
            if b["active"]:
                took = time.time() - t0; break
            await asyncio.sleep(0.05)
        a = await phone.st()
        check("3. iPad takes over < 1 s, same highlight, iPhone goes quiet",
              took is not None and took < 1 and b["hl"] == before and not a["active"],
              f"{took and round(took*1000)} ms, hl {before}->{b['hl']}, iPhone active={a['active']}")
        await fake(http, "NOD"); await asyncio.sleep(0.6)
        a, b = await phone.st(), await ipad.st()
        check("   after switch only the iPad speaks", len(b["spoken"]) == 1 and len(a["spoken"]) == 1,
              f"iPad said {b['spoken']}, iPhone total {a['spoken']}")

        # Test 5: refresh -> same state
        await fake(http, "RIGHT"); await fake(http, "RIGHT"); await asyncio.sleep(0.4)
        hl = (await phone.st())["hl"]
        await phone.cdp("Page.reload", ignoreCache=True); await asyncio.sleep(2)
        a = await phone.st()
        check("5. refresh comes back in the same state", a["hl"] == hl and not a["active"] and "iPad" in a["bar"],
              f"hl {hl}->{a['hl']}, bar '{a['bar']}'")

        # Test 4: active iPad disappears -> released after 10 s -> iPhone takes over with one tap
        await ipad.browser("Target.closeTarget", targetId=ipad.target)
        await asyncio.sleep(4)
        a = await phone.st()
        still = a["activeName"]
        await asyncio.sleep(8)
        a = await phone.st()
        check("4. active device gone 10 s -> released", still == "iPad" and not a["activeName"], f"at 4 s: {still}, at 12 s: {a['activeName']} | bar '{a['bar']}'")
        await phone.ev("document.getElementById('talkHere').click()"); await asyncio.sleep(0.6)
        a = await phone.st()
        check("   one tap on the iPhone takes over", a["active"], a["bar"])

        errs = phone.errors + ipad.errors
        check("no JavaScript errors", not errs, "; ".join(errs[:3]))
        await bws.close()


asyncio.run(main())
print("\nSUMMARY:", sum(ok for _, ok, _ in results), "/", len(results), "passed")
