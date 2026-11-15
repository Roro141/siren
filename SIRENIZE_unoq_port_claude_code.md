# Task for Claude Code: Move Sirenize onto the Arduino UNO Q (no laptop)

## Context
Everything works today on: GY-521 → **Arduino Mega** (sketch `tilt_test/tilt_test.ino`, prints `EVENT LEFT/RIGHT/NOD` over USB serial) → **laptop** running `server.py` (aiohttp: WebSocket to the phone, `/tts` ElevenLabs, `/predict` + `/ask` Gemini, `/dashboard`, `/log`, `/stats`, `/fake/<EVENT>`) → cloudflared HTTPS tunnel → **iPhone** page.

We now have an **Arduino UNO Q**: a Linux side (Qualcomm, Debian, Wi-Fi) plus a microcontroller side (STM32, runs sketches). Its headers and Qwiic port are **3.3 V**. The microcontroller has no USB of its own; it talks to Linux through the **Bridge** (RPC).

**Goal:** the UNO Q reads the sensor *and* runs `server.py` + the tunnel, powered by a USB-C power bank, connected to my iPhone's hotspot. The laptop only opens `/dashboard` as a viewer.

## Ground rules
- **Do not touch or break the Mega + laptop setup.** It's our demo backup. Work on a copy of the project. Hard deadline: if this isn't passing by **7:00 AM**, stop and tell me.
- Ask before installing anything, on the laptop or the board.
- **The Bridge API and App Lab app layout:** check the current Arduino UNO Q docs and the example apps on the board before writing code. Don't guess function names. Show me what you found.
- Never print API keys. `.env` gets copied to the board but never committed.

---

## Step 0: What I do by hand first (walk me through it, then wait)
1. Install **Arduino App Lab** on the Mac, connect the UNO Q by USB-C, and complete first-time setup: board name **`sirenize`**, a password, Wi-Fi = **my iPhone hotspot**, and any system update it offers.
2. Rewire the GY-521 to the UNO Q:
   - VCC → **3.3V** (not 5V)
   - GND → GND
   - SDA → SDA
   - SCL → SCL (the I²C pins on the UNO headers; confirm their location on the official pinout and tell me)
3. Tell me the board's IP (App Lab shows it), or try `sirenize.local`.

## Step 1: Get a shell on the board
- Try `ssh arduino@sirenize.local` (or the IP). If SSH isn't available, use what App Lab / the docs recommend (e.g. `adb`), and explain.
- Report: OS version, Python version, free disk, RAM, and whether `pip`/`venv` work.
- Make a working dir `~/sirenize/` and copy the project there (`server.py`, `public/`, `phrases.json`, `profile.json`, `.env`, audio cache). **Exclude** `.git` and the Arduino folders. Use `rsync` or `scp`.

## Step 2: The sensor sketch on the UNO Q microcontroller
- Port `tilt_test.ino` into an App Lab app (the docs show the folder layout; typically a sketch part plus a Python part).
- Keep **all the detection logic and constants the same**. Only change the output: instead of printing `EVENT LEFT`, send the event to Linux over the **Bridge** (e.g. a notify/call named `gesture` with `"LEFT"`, `"RIGHT"`, `"NOD"`; use the real API from the docs).
- Keep a periodic heartbeat (e.g. every 2 s) so Linux knows the sensor is alive.
- If the I²C bus or the address isn't found, check the wiring with me before changing code.
- Optional, if quick: use the LED matrix as a status light (a dot when ready, an "X" if the sensor isn't found).

## Step 3: Connect Bridge → the existing server (minimal changes)
Keep `server.py` almost unchanged. Prefer this design:
- The **Python side of the App Lab app** receives Bridge `gesture` events and forwards each one to the server with a local HTTP call: `POST http://127.0.0.1:8080/event/<EVENT>`. Add that endpoint to `server.py`; it does exactly what `/fake/<EVENT>` does but is marked `source: sensor`.
- Add a `--source bridge` mode to `server.py` that skips opening a serial port. On the Mega/laptop setup, the default `--source serial` still works unchanged.
- Heartbeats update the dashboard's **Sensor** health dot.
- Run `server.py` in its own venv as a normal service (not inside App Lab), so it isn't coupled to the sketch.

## Step 4: Internet, HTTPS and a stable link for the phone
- Install the **arm64** `cloudflared` on the board (ask first) and run the quick tunnel.
- **Problem:** the quick-tunnel URL changes on every boot. Solve it one of these ways, and recommend which:
  - **A.** The server serves a tiny **plain-HTTP** page at `http://sirenize.local:8080/go` that redirects to the current tunnel URL. I bookmark that on the phone. (Test whether the iPhone resolves `sirenize.local` while it's the hotspot host. If not, use the IP.)
  - **B.** An ngrok free account with its one free static domain (a fixed HTTPS URL). Ask me before creating anything.
- The dashboard shows the current public URL. If you added the LED matrix status, it shows a check mark when the tunnel is up.

## Step 5: Start everything on power-up
- `systemd` services for: `server.py --source bridge`, the tunnel, and the App Lab app / Bridge forwarder (or set the app to run at startup if App Lab supports it; check the docs).
- Order: Wi-Fi up → server → tunnel. Restart on failure.
- **Goal:** I press the power button, wait about 60 s, open the bookmark on the phone, and it works.

## Step 6: Test with me (PASS/FAIL each)
1. **Sensor:** 10 LEFT, 10 RIGHT, 10 NOD on the cap. All 30 arrive on the phone, with no doubles.
2. **Full flow:** the camera sees a cup → "I'm thirsty" is suggested → a nod → the cloned voice speaks it.
3. **Ask a question:** from the dashboard on the laptop → answered with tilts.
4. **Cold boot:** unplug the power, plug it back in, and **without touching the laptop**, everything works within about 90 s via the bookmark. Report the time.
5. **Battery:** 20 minutes on the power bank with continuous use. No reboots. Check `dmesg` for under-voltage warnings and report the CPU temperature.
6. **Wi-Fi drop:** turn the hotspot off for 30 s and back on. The server recovers, and cached phrases kept playing on the phone the whole time.
7. **Backup still works:** confirm the Mega + laptop setup still runs unchanged.

## Final report
A PASS/FAIL table, the boot-to-ready time, how I start it (one sentence), what to do if the phone link doesn't open, and anything that's still fragile.
