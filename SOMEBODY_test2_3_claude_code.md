# Task for Claude Code: SOMEBODY Tests 2 + 3 (tilt → laptop → phone)

Context: Test 1 is done. The Mega 2560 is running `tilt_test/tilt_test.ino`, which prints lines like `EVENT LEFT  #3 ...`, `EVENT RIGHT ...` and `EVENT NOD ...` over serial at 115200. Don't re-flash it.

Goal: a head tilt moves a highlight on a web page on my **iPhone**, and a nod "says" the phrase out loud. The laptop is only a relay.

## Ground rules
- Ask me before installing anything (pip, brew).
- The Arduino IDE Serial Monitor must be closed (only one program can use the port).
- Keep it simple: one Python server file plus one HTML file. No frameworks, no build step.

## Step 1: Server (`server.py`, Python 3, `aiohttp` + `pyserial`)
- Auto-find the Mega's port (`/dev/cu.usbmodem*`, `/dev/cu.usbserial*`, `/dev/cu.wchusbserial*`), or take `--port`.
- Read serial in a background thread. Parse lines starting with `EVENT ` into `LEFT`, `RIGHT` or `NOD`.
- Serve `public/index.html` at `/`, and a WebSocket at `/ws` that broadcasts `{"type": "LEFT", "server_ms": <epoch ms>}` to every connected page.
- `GET /fake/LEFT` (and `/fake/RIGHT`, `/fake/NOD`) broadcasts a fake event, for testing without the headband.
- On the WebSocket, answer `{"ping": n}` with `{"pong": n}` so the page can measure round-trip time.
- If the serial port disconnects, keep the server up, retry the port every 2 s, and log it.
- Print every event to the terminal with a timestamp.
- Run on port 8080.

## Step 2: Phone page (`public/index.html`, plain HTML/JS, big and readable on a phone)
- A big **Start** button first. Tapping it unlocks audio on iOS (speak an empty utterance) and starts the camera.
- A 2×3 grid of large phrase tiles: "I need water", "Bathroom", "I'm in pain", "Yes", "No", "Thank you".
- One tile is highlighted at a time.
  - `RIGHT` moves the highlight to the next tile (wrap around).
  - `LEFT` moves it to the previous tile.
  - `NOD` selects the tile: flash it, show the phrase huge at the top, and speak it with `speechSynthesis`. This is a placeholder; ElevenLabs comes later.
- Small live camera preview in a corner (`getUserMedia`, back camera `facingMode: "environment"`). It isn't used yet; this only proves camera access works.
- A status pill: green "Connected" or red "Reconnecting…". Auto-reconnect the WebSocket every 1 s.
- Tiny debug footer: counts of LEFT/RIGHT/NOD received, and round-trip ms (ping every 2 s).
- Keyboard fallback for laptop testing: ← → arrows and the space bar act as LEFT / RIGHT / NOD.

## Step 3: Get it onto the phone over HTTPS
iPhone Safari only allows the camera on HTTPS pages.
1. Ask me, then `brew install cloudflared` if it's missing.
2. Run `cloudflared tunnel --url http://localhost:8080` and give me the `https://….trycloudflare.com` link.
3. I open it on the phone. The phone only needs internet, not the same Wi-Fi.

If the tunnel won't work (venue network), fall back to the laptop's LAN IP over plain `http://` with both devices on my phone's hotspot. Events and audio will still work there; only the camera won't.

Write a `run.sh` that starts the server and the tunnel together and prints the link.

## Step 4: Test with me. Report PASS/FAIL for each.
1. **Fake events:** I open the page on the phone and tap Start. You hit `/fake/RIGHT` 5 times. The highlight moves 5 tiles.
2. **Real tilts:** I do 20 tilts (mix of left and right) on the headband. You check the terminal log and I watch the phone. **Pass:** 20 sent, 20 received, no doubles, and it feels instant (round-trip under about 300 ms).
3. **Select + speak:** I nod on "I need water". **Pass:** it flashes, shows the phrase and speaks it out loud from the phone.
4. **Camera:** the preview shows live video.
5. **Reconnect:** you stop and restart the server. **Pass:** the page goes red, then back to green on its own, and tilts work again.
6. **Unplug:** I unplug the Mega's USB for 5 s and plug it back in. **Pass:** the server recovers without a restart.

## Notes
- Remind me to set the iPhone's **Settings → Display & Brightness → Auto-Lock → Never** for testing.
- If round-trip over the tunnel is too slow (over 300 ms), test the hotspot LAN option and tell me both numbers.

## Final report
A PASS/FAIL table for tests 1 to 6, the measured round-trip ms, and the exact command(s) to start everything again later.
