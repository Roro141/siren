// SOMEBODY - Test 1: can we reliably read a head tilt?
// Hardware: Elegoo Mega 2560 + GY-521 (MPU-6050). No libraries needed.
//
// WIRING (GY-521 -> Mega)
//   VCC -> 5V      (GY-521 has its own regulator)
//   GND -> GND
//   SCL -> pin 21 (SCL)
//   SDA -> pin 20 (SDA)
//   (leave XDA, XCL, AD0, INT unconnected)
//
// MOUNTING: tape the board FLAT on TOP of the head (chip facing the ceiling),
// on a headband or cap. If LEFT/RIGHT come out swapped, set FLIP_ROLL = true.
// If a forward nod doesn't register but leaning back does, set FLIP_PITCH = true.
//
// SERIAL MONITOR: 115200 baud. Type a letter and press Enter:
//   c = recalibrate (hold head still and neutral for 1 second)
//   m = monitor mode (events + counts + stillness checks)  [default]
//   p = plotter mode (open Tools > Serial Plotter to see live angles)
//   r = reset counters
//   s = print summary

#include <Wire.h>
#include <math.h>

// ---- Tuning knobs ----
const float TILT_ON   = 12.0;            // degrees away from neutral to trigger
const float TILT_OFF  = 7.0;             // must come back within this to re-arm
const unsigned long HOLD_MS = 150;       // must hold past TILT_ON this long
const float ALPHA = 0.2;                 // smoothing (0..1, lower = smoother)
const bool FLIP_ROLL  = false;
const bool FLIP_PITCH = false;
const unsigned long SAMPLE_MS = 20;      // 50 Hz
const unsigned long STILL_REPORT_MS = 5000;

const uint8_t MPU = 0x68;

float roll = 0, pitch = 0;               // smoothed absolute angles
float roll0 = 0, pitch0 = 0;             // neutral (from calibration)
bool plotMode = false;

enum Gesture { NONE = 0, LEFT = 1, RIGHT = 2, NOD = 3 };
const char* NAMES[] = { "NONE", "LEFT", "RIGHT", "NOD" };

bool armed = true;
Gesture pending = NONE;
unsigned long pendingSince = 0;
int counts[4] = { 0, 0, 0, 0 };

float maxStillRoll = 0, maxStillPitch = 0;
unsigned long lastStillReport = 0, lastSample = 0, lastErr = 0;

// ---------------------------------------------------------------------------
bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool readAngles(float &r, float &p) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);                      // ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU, (uint8_t)6) != 6) return false;
  int16_t x = (Wire.read() << 8) | Wire.read();
  int16_t y = (Wire.read() << 8) | Wire.read();
  int16_t z = (Wire.read() << 8) | Wire.read();
  float ax = x / 16384.0, ay = y / 16384.0, az = z / 16384.0;
  r = atan2(ay, az) * 180.0 / PI;
  p = atan2(-ax, sqrt(ay * ay + az * az)) * 180.0 / PI;
  return true;
}

void calibrate() {
  Serial.println(F("\n>> Calibrating: hold head STILL and NEUTRAL..."));
  delay(500);
  float sr = 0, sp = 0; int n = 0;
  for (int i = 0; i < 50; i++) {
    float r, p;
    if (readAngles(r, p)) { sr += r; sp += p; n++; }
    delay(20);
  }
  if (n == 0) { Serial.println(F("!! Calibration failed: no sensor data. Check wiring.")); return; }
  roll0 = roll = sr / n;
  pitch0 = pitch = sp / n;
  armed = true; pending = NONE;
  maxStillRoll = maxStillPitch = 0;
  lastStillReport = millis();
  Serial.print(F(">> Neutral set: roll=")); Serial.print(roll0, 1);
  Serial.print(F(" pitch=")); Serial.println(pitch0, 1);
}

void printSummary() {
  Serial.println(F("\n===== SUMMARY ====="));
  for (int g = 1; g <= 3; g++) {
    Serial.print(NAMES[g]); Serial.print(F(": ")); Serial.println(counts[g]);
  }
  Serial.println(F("Pass = 10/10 for each gesture, per person, and"));
  Serial.println(F("0 events during 30 s of sitting still + talking."));
  Serial.println(F("===================\n"));
}

void fire(Gesture g, float dr, float dp) {
  counts[g]++;
  if (plotMode) return;
  Serial.print(F("EVENT ")); Serial.print(NAMES[g]);
  Serial.print(F("  #")); Serial.print(counts[g]);
  Serial.print(F("   d_roll=")); Serial.print(dr, 1);
  Serial.print(F(" d_pitch=")); Serial.print(dp, 1);
  Serial.print(F("   t=")); Serial.print(millis()); Serial.println(F("ms"));
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    switch (c) {
      case 'c': case 'C': calibrate(); break;
      case 'p': case 'P': plotMode = true; break;
      case 'm': case 'M': plotMode = false; Serial.println(F(">> Monitor mode")); break;
      case 'r': case 'R':
        for (int i = 0; i < 4; i++) counts[i] = 0;
        Serial.println(F(">> Counters reset")); break;
      case 's': case 'S': printSummary(); break;
    }
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  Wire.begin();
#if defined(WIRE_HAS_TIMEOUT)
  Wire.setWireTimeout(3000, true);       // don't freeze if a wire comes loose
#endif
  delay(200);

  Serial.println(F("\nSOMEBODY tilt test"));
  if (!writeReg(0x6B, 0x00)) {           // PWR_MGMT_1: wake up
    Serial.println(F("!! No response from GY-521 at 0x68. Check VCC/GND/SDA(20)/SCL(21)."));
  }
  writeReg(0x1C, 0x00);                  // accel range +-2g
  writeReg(0x1A, 0x03);                  // on-chip low-pass filter ~44 Hz

  Wire.beginTransmission(MPU); Wire.write(0x75); Wire.endTransmission(false);
  Wire.requestFrom(MPU, (uint8_t)1);
  if (Wire.available()) {
    uint8_t who = Wire.read();
    Serial.print(F("WHO_AM_I = 0x")); Serial.println(who, HEX);
    if (who != 0x68) Serial.println(F("(not 0x68: clone chip, usually still fine)"));
  }

  calibrate();
  Serial.println(F("Commands: c=calibrate m=monitor p=plotter r=reset s=summary\n"));
}

void loop() {
  handleSerial();
  unsigned long now = millis();
  if (now - lastSample < SAMPLE_MS) return;
  lastSample = now;

  float r, p;
  if (!readAngles(r, p)) {
    if (now - lastErr > 1000) { Serial.println(F("!! Sensor read failed. Check wiring.")); lastErr = now; }
    return;
  }
  roll  = ALPHA * r + (1 - ALPHA) * roll;
  pitch = ALPHA * p + (1 - ALPHA) * pitch;

  float dr = roll - roll0;   if (FLIP_ROLL)  dr = -dr;
  float dp = pitch - pitch0; if (FLIP_PITCH) dp = -dp;

  // Which gesture (if any) is past the trigger threshold? Biggest wins.
  Gesture cand = NONE;
  if (fabs(dr) >= TILT_ON && fabs(dr) >= fabs(dp)) cand = (dr < 0) ? LEFT : RIGHT;
  else if (dp >= TILT_ON) cand = NOD;

  if (armed) {
    // Track wobble while "at rest" for the stillness check
    if (cand == NONE) {
      if (fabs(dr) > maxStillRoll)  maxStillRoll  = fabs(dr);
      if (fabs(dp) > maxStillPitch) maxStillPitch = fabs(dp);
    }
    if (cand != NONE) {
      if (cand != pending) { pending = cand; pendingSince = now; }
      else if (now - pendingSince >= HOLD_MS) { fire(cand, dr, dp); armed = false; }
    } else {
      pending = NONE;
    }
  } else if (fabs(dr) < TILT_OFF && fabs(dp) < TILT_OFF) {
    armed = true; pending = NONE;        // back to neutral: ready for next gesture
  }

  if (plotMode) {
    Serial.print(F("roll:"));   Serial.print(dr, 1);
    Serial.print(F(",pitch:")); Serial.print(dp, 1);
    Serial.print(F(",on:"));    Serial.print(TILT_ON, 0);
    Serial.print(F(",off:"));   Serial.println(-TILT_ON, 0);
  } else if (now - lastStillReport >= STILL_REPORT_MS) {
    Serial.print(F("  still-check (last 5s, at rest): max wobble roll="));
    Serial.print(maxStillRoll, 1); Serial.print(F(" pitch="));
    Serial.print(maxStillPitch, 1);
    Serial.println((maxStillRoll < 5 && maxStillPitch < 5) ? F("  OK") : F("  HIGH (want < 5)"));
    maxStillRoll = maxStillPitch = 0;
    lastStillReport = now;
  }
}
