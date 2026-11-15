# SOMEBODY: head-tilt communication glasses

A head-worn tilt sensor lets someone pick and speak a phrase with no hands and no voice of their own:

- **Tilt left / right** moves a highlight across phrase tiles on a phone.
- **Nod** selects the tile, which is then shown large and spoken out loud.

```
GY-521 (MPU-6050) on glasses ──I2C──▶ Elegoo Mega 2560 ──USB serial──▶ laptop (server.py)
                                                                          │ WebSocket
                                                       Cloudflare tunnel (HTTPS) ▼
                                                                     iPhone Safari page
```

The laptop is only a relay. The phone page does the UI and speech.

---

## Hardware

| Part | Notes |
|---|---|
| Elegoo Mega 2560 | Any Arduino Mega works |
| GY-521 (MPU-6050) | Accelerometer only is used; no libraries needed |
| 4 jumper wires | Female-to-female at the sensor end, with strain relief |
| Glasses (or headband) | Sensor taped to the arm, near the hinge |

### Wiring (GY-521 → Mega)

| GY-521 | Mega |
|---|---|
| VCC | 5V |
| GND | GND |
| SDA | pin 20 (SDA) |
| SCL | pin 21 (SCL) |

Leave XDA, XCL, AD0 and INT unconnected.

### Mounting: orientation matters

The sketch reads **roll** for left/right and **pitch** for nod, so the board's printed axes must line up with your head:

- The board lies **flat** when worn, with the chip facing up.
- The **X arrow points front/back**, along the direction your nose points.
- The **Y arrow points out to the side**.

The tested mount (sensor taped to the glasses arm) needed `FLIP_ROLL = false` and `FLIP_PITCH = false`. If you mount it differently:

| Symptom | Fix |
|---|---|
| Nods come out as LEFT/RIGHT, or tilts as NOD | Board is a quarter-turn off. Rotate it 90° flat. |
| Left and right swapped, nods ignored | Board is 180° off. Spin it 180°, or flip both `FLIP_*` constants. |
| Only left/right swapped | Toggle `FLIP_ROLL` |
| Nods ignored but leaning back triggers | Toggle `FLIP_PITCH` |

**Mark the FRONT on the board** so it always goes back the same way.

---

## Files

| File | What it is |
|---|---|
| `tilt_test/tilt_test.ino` | Mega sketch: reads the MPU-6050, prints `EVENT LEFT/RIGHT/NOD` over serial (115200) |
| `server.py` | Relay: serial → WebSocket, serves the phone page, auto-reconnects to the Mega |
| `public/index.html` | Phone page: phrase grid, highlight, speech, camera preview, status pill |
| `run.sh` | Starts the server and a Cloudflare tunnel, prints the phone link |
| `serial_log.py` | Test 1 helper: log serial for N seconds, send commands |
| `serial_session.py` | Test 1 helper: persistent serial connection (avoids resetting the Mega every run) |
| `SOMEBODY_test1_claude_code.md` | Test 1 plan (tilt detection) |
| `SOMEBODY_test2_3_claude_code.md` | Tests 2 + 3 plan (tilt → laptop → phone) |

---

## Setup

Tested on macOS (Apple Silicon), Python 3.9.

```bash
# 1. Python packages
pip3 install --user pyserial aiohttp

# 2. Flash the Mega (arduino-cli, or the copy bundled with Arduino IDE 2)
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:mega tilt_test
arduino-cli upload  --fqbn arduino:avr:mega -p /dev/cu.usbmodemXXXX tilt_test

# 3. cloudflared (for the HTTPS link the iPhone camera needs).
#    Either `brew install cloudflared`, or put the official binary in ./bin/:
mkdir -p bin && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz | tar -xz -C bin
```

Close the Arduino IDE Serial Monitor first: only one program can use the port at a time.

## Run

```bash
./run.sh            # or: ./run.sh --port /dev/cu.usbmodemXXXX
```

It prints:

- **Phone link (HTTPS):** a new `https://….trycloudflare.com` link each run. Open it in Safari on the phone and tap **Start**.
- **Hotspot/LAN fallback:** `http://<laptop-ip>:8080`. Everything works except the camera, because iPhone Safari only allows the camera on HTTPS pages.

**Ctrl+C** stops the server and the tunnel.

Useful while it runs:

```bash
curl localhost:8080/fake/RIGHT    # fake event: LEFT, RIGHT or NOD (test without the glasses)
curl localhost:8080/calibrate     # re-zero the sensor; wearer holds still and looks straight ahead
```

On the phone, set **Settings → Display & Brightness → Auto-Lock → Never** while testing.

### Phone page

- 2×3 tiles: *I need water, Bathroom, I'm in pain, Yes, No, Thank you*
- **RIGHT** moves to the next tile, **LEFT** to the previous one (both wrap around). **NOD** flashes the tile, shows the phrase large and speaks it (`speechSynthesis`, a placeholder for ElevenLabs).
- Status pill:
  - 🟢 **Connected**
  - 🟠 **Headband disconnected** (Mega unplugged)
  - 🔴 **Reconnecting…** (server unreachable; retries every 1 s)
- Footer shows LEFT/RIGHT/NOD counts and round-trip ms (pinged every 2 s).
- Small back-camera preview, which only proves camera access for now.
- Keyboard fallback on a laptop: ← → and Space.

### Sketch serial commands (115200 baud)

| Key | Action |
|---|---|
| `c` | Recalibrate: hold still and look straight ahead for about 1.5 s |
| `m` | Monitor mode (events, counts, stillness checks); the default |
| `p` | Plotter mode (live angles) |
| `r` | Reset counters |
| `s` | Print summary |

### Final tuning constants

```cpp
const float TILT_ON   = 12.0;   // degrees from neutral to trigger (was 15)
const float TILT_OFF  = 7.0;    // must return within this to re-arm
const unsigned long HOLD_MS = 150;
const float ALPHA = 0.2;
const bool FLIP_ROLL  = false;  // for the glasses-arm mount
const bool FLIP_PITCH = false;
```

---

## Test results

### Test 1: can we reliably read a head tilt? (person 1, glasses mount)

| Gesture | Done | Detected | Wrong events | Result |
|---|---|---|---|---|
| LEFT | 10 | 9 | 1 NOD (looked down at the screen) | FAIL |
| RIGHT | 10 | 9 | 0 | FAIL |
| NOD | 9 | 9 | 0 | PASS |
| Stillness (30 s talking) | — | 0 events, max wobble 4.6° | — | PASS |
| Recalibration time | — | about 1.5 s | — | PASS (limit 30 s) |

**Verdict: GO with button fallback.** The hardware works, is quiet at rest and never confused left with right. The misses were always the **first gesture of a round**, which peaked at 11.6–11.8°, just under the 12° trigger. All later gestures reached 20–27°. Person 2 was not tested.

Tuning path (4 attempts):
1. `FLIP_ROLL = true`
2. `TILT_ON` 15 → 12
3. `FLIP_PITCH = true`
4. Both flips back to `false`, after moving the sensor to the glasses arm (board 180° from before)

### Tests 2 + 3: tilt → laptop → phone

| # | Test | Result |
|---|---|---|
| 1 | Fake events (5 × `/fake/RIGHT`) | **PASS**: 5 sent, 5 received |
| 2 | 20+ real tilts | **PASS**: 22 sent, 22 received, no doubles |
| 3 | Nod on "I need water" | **PASS**: flashed, shown large, spoken aloud |
| 4 | Camera preview | **PASS**: live video (back camera) |
| 5 | Server stopped and restarted | **PASS**: page went red, reconnected on its own, tilts worked |
| 6 | Mega USB unplugged and replugged | **PASS**: server recovered without a restart; page shows 🟠 while unplugged |

**Round-trip, phone ↔ server over the Cloudflare tunnel:** median **43 ms** (15–320 ms, 28 pings). That's well under the 300 ms target.

---

## Lessons learned / known issues

- **Opening the serial port resets the Mega**, which recalibrates immediately. The wearer must hold still for about 2 s whenever the server connects or the USB is replugged. Use `curl localhost:8080/calibrate` to re-zero once they're settled.
- **Calibrate in the pose you'll use.** Calibrating while looking down at a screen, then looking up, leaves a 20° offset. The sketch then never re-arms, because it needs to get back within `TILT_OFF`.
- **Looking down at the phone counts as a NOD.** Mount the phone at eye level. A future fix could be a "hold to confirm" step before selecting.
- **Loose wires freeze the sensor.** A brief VCC dropout puts the MPU-6050 back to sleep, and the sketch only wakes it at boot, so readings freeze. Solder the header pins and tape the connectors with strain relief.
- **Mount orientation** is the most common failure. See the table under [Mounting](#mounting-orientation-matters).
- The first gesture of a session tends to be tentative. Lowering `TILT_ON` to about 10 is worth trying.

## Next steps

- ElevenLabs voice in place of `speechSynthesis`
- Person 2 run of Test 1
- Use the camera (currently only a preview)
- Optional button fallback for selection
