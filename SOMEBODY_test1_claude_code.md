# Task for Claude Code: flash and run SOMEBODY Test 1 (head-tilt detection)

You are running in a terminal on my Mac (Apple Silicon). An Elegoo Mega 2560 is plugged in over USB with a GY-521 (MPU-6050) wired to it. Your job: compile and upload the sketch, then run the tilt test with me and tell me if it passes. I do the physical gestures; you do everything on the computer.

## Ground rules
- Ask me before installing anything (Homebrew packages, pip packages, Arduino cores).
- Do not change the sketch's logic. You may only change the tuning constants at the top (`TILT_ON`, `TILT_OFF`, `HOLD_MS`, `ALPHA`, `FLIP_ROLL`, `FLIP_PITCH`), one at a time, and tell me what you changed and why.
- Make sure the Arduino IDE's Serial Monitor is closed before uploading or reading serial. Only one program can use the port at a time.

## Step 1: Find the sketch
The sketch must live at `tilt_test/tilt_test.ino` (the folder name has to match the file name). I've put `tilt_test.ino` in this folder or in `~/Downloads`. Find it, and if it isn't already inside a `tilt_test/` folder, create one in the current directory and move it there.

## Step 2: Get arduino-cli
1. Check `which arduino-cli`.
2. If it's missing, check for the copy bundled with Arduino IDE 2:
   `"/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli" version`
   If that works, use that path (alias it as `ACLI` in your commands).
3. If neither exists, ask me before running `brew install arduino-cli`.

Then:
```
arduino-cli core update-index
arduino-cli core install arduino:avr     # skip if `arduino-cli core list` already shows arduino:avr
```

## Step 3: Find the board's port
Run `arduino-cli board list`. Look for `/dev/cu.usbmodem…` or `/dev/cu.usbserial…` / `/dev/cu.wchusbserial…`.
- If there's no such port, tell me to (a) try another USB cable, since some cables are charge-only, and (b) unplug and replug. If it's still missing, the board probably uses a CH340 USB chip. Tell me it may need the WCH CH340 macOS driver, and don't install drivers yourself.

## Step 4: Compile and upload
```
arduino-cli compile --fqbn arduino:avr:mega tilt_test
arduino-cli upload  --fqbn arduino:avr:mega -p <PORT> tilt_test
```
If compile fails, show me the error and fix it, keeping the logic the same. If upload fails with "port busy" or "resource busy", ask me to close the Arduino IDE Serial Monitor.

## Step 5: A serial helper you can drive
`arduino-cli monitor` is interactive and awkward for you to use, so write a small Python script `serial_log.py`:
- Uses `pyserial` (ask me before `pip3 install pyserial` if it's missing).
- Usage: `python3 serial_log.py <PORT> <seconds> [command_chars]`
- Opens the port at 115200, waits 2 s (opening the port resets the Mega), optionally sends the command characters (e.g. `r`, `c`, `s`), one per line with a short pause between, then prints every line received for `<seconds>` seconds and exits.
- Also appends everything to `tilt_log.txt` with timestamps.

The sketch understands these commands: `c` = recalibrate (I must hold still and neutral), `m` = monitor mode, `p` = plotter mode, `r` = reset counters, `s` = print summary.

## Step 6: Sanity check
Run the helper for about 8 seconds. Expect `SOMEBODY tilt test`, `WHO_AM_I = 0x68` (another value is usually a clone chip and still fine), and `Neutral set`.
- If you see `No response from GY-521` or `Sensor read failed`, stop and tell me to check the wiring: VCC→5V, GND→GND, SDA→pin 20, SCL→pin 21.

## Step 7: Run the test with me (once per person, two people total)
Walk me through each round. Tell me exactly what to do, then start logging, then report the result.

1. **Calibrate:** tell me to sit normally with the sensor flat on top of my head, then send `c` and log for 4 s.
2. **Left:** send `r`, then tell me: "Do 10 LEFT tilts, back to neutral each time, about 2 s apart." Log for 35 s, then send `s`.
3. **Right:** same as step 2 with RIGHT tilts.
4. **Nod:** same as step 2 with forward NODs.
5. **Stillness:** send `r`, tell me "Sit still and talk normally for 30 s." Log for 35 s, then send `s`. Also read the `still-check` lines.

After each round, count the `EVENT` lines and check that they're the right gesture.

## Pass criteria (report each as PASS/FAIL)
- 10/10 LEFT, 10/10 RIGHT, 10/10 NOD for each person, with no wrong-gesture events.
- 0 events during the 30 s stillness round, and `still-check` wobble under 5° (lines say `OK`).
- Recalibrating for a new person takes under 30 s.

## If it's close but not passing (tuning loop)
- LEFT/RIGHT swapped → `FLIP_ROLL = true`. Nods not detected but leaning back is → `FLIP_PITCH = true`.
- Missed gestures → lower `TILT_ON` (15 → 12).
- False triggers or doubled events → raise `TILT_ON` (15 → 18) or `HOLD_MS` (150 → 250).
- Change one constant, re-upload, re-run only the failing round. Stop after 4 tuning attempts and summarize.

## Final report
Give me a short table: person × gesture → count, the stillness result, the final constant values, and an overall verdict: **GO** (all pass), **GO with button fallback** (tilt is unreliable but the hardware works), or **NO-GO** (sensor can't be read at all).
