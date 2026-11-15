// SIRENIZE - tilt sensor on the Arduino UNO Q microcontroller (STM32).
// Port of tilt_test/tilt_test.ino: identical detection logic and constants.
// Only the output changed: events go to Linux over the Bridge instead of Serial.
//
//   Bridge.notify("gesture", "LEFT" | "RIGHT" | "NOD")
//   Bridge.notify("heartbeat", sensorOk)          every 2 s
//   Bridge.notify("status", "<text>")             calibration / errors
//   Bridge.provide("calibrate", ...)              Linux asks for a re-zero
//
// WIRING (GY-521 -> UNO Q, 3.3 V logic): VCC->3.3V, GND->GND, SDA->SDA, SCL->SCL.
// The header SDA/SCL pins are Wire; the Qwiic connector is Wire1. Both are tried.

#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include <math.h>

// ---- Tuning knobs (same values as the tested Mega setup) ----
const float TILT_ON   = 12.0;            // degrees away from neutral to trigger
const float TILT_OFF  = 7.0;             // must come back within this to re-arm
const unsigned long HOLD_MS = 150;       // must hold past TILT_ON this long
const float ALPHA = 0.2;                 // smoothing (0..1, lower = smoother)
const bool FLIP_ROLL  = false;
const bool FLIP_PITCH = false;
const unsigned long SAMPLE_MS = 20;      // 50 Hz
const unsigned long HEARTBEAT_MS = 2000;

const uint8_t MPU_ADDR = 0x68;   // "MPU" is a CMSIS macro on Cortex-M33
TwoWire *bus = &Wire;

float roll = 0, pitch = 0;               // smoothed absolute angles
float roll0 = 0, pitch0 = 0;             // neutral (from calibration)

enum Gesture { NONE = 0, LEFT = 1, RIGHT = 2, NOD = 3 };
const char* NAMES[] = { "NONE", "LEFT", "RIGHT", "NOD" };

bool armed = true;
Gesture pending = NONE;
unsigned long pendingSince = 0;
int counts[4] = { 0, 0, 0, 0 };

bool sensorOk = false;
volatile bool calibrateRequested = false;
unsigned long lastSample = 0, lastErr = 0, lastHeartbeat = 0, lastScan = 0;

// ---------------------------------------------------------------------------
bool writeReg(uint8_t reg, uint8_t val) {
  bus->beginTransmission(MPU_ADDR);
  bus->write(reg);
  bus->write(val);
  return bus->endTransmission() == 0;
}

// A sleeping MPU-6050 (after a power dip) returns all zeros or the exact same raw
// values forever. Real readings always flicker in the low bits.
int16_t lastX = 0, lastY = 0, lastZ = 0;
int sameCount = 0;
const int FROZEN_SAMPLES = 25;           // 0.5 s of identical raw data at 50 Hz

bool readAngles(float &r, float &p) {
  bus->beginTransmission(MPU_ADDR);
  bus->write(0x3B);                      // ACCEL_XOUT_H
  if (bus->endTransmission(false) != 0) return false;
  if (bus->requestFrom((uint8_t)MPU_ADDR, (uint8_t)6) != 6) return false;
  int16_t x = (bus->read() << 8) | bus->read();
  int16_t y = (bus->read() << 8) | bus->read();
  int16_t z = (bus->read() << 8) | bus->read();
  if (x == 0 && y == 0 && z == 0) return false;          // asleep / no data
  if (x == lastX && y == lastY && z == lastZ) { if (++sameCount >= FROZEN_SAMPLES) return false; }
  else sameCount = 0;
  lastX = x; lastY = y; lastZ = z;
  float ax = x / 16384.0, ay = y / 16384.0, az = z / 16384.0;
  r = atan2(ay, az) * 180.0 / PI;
  p = atan2(-ax, sqrt(ay * ay + az * az)) * 180.0 / PI;
  return true;
}

bool busesStarted = false;
void startBuses() {
  if (busesStarted) return;
  Wire.begin();
  Wire1.begin();
  busesStarted = true;
}

bool probe(TwoWire *w) {
  w->beginTransmission(MPU_ADDR);
  return w->endTransmission() == 0;
}

// Diagnostic: list every I2C address that answers on a bus.
String scan(TwoWire *w) {
  String found = "";
  for (uint8_t a = 0x08; a < 0x78; a++) {
    w->beginTransmission(a);
    if (w->endTransmission() == 0) { found += " 0x"; found += String(a, HEX); }
  }
  return found.length() ? found : String(" none");
}

// Wake the MPU-6050 (also after a power glitch, which puts it back to sleep).
bool initSensor() {
  startBuses();
  if (probe(&Wire)) bus = &Wire;
  else if (probe(&Wire1)) bus = &Wire1;
  else return false;
  if (!writeReg(0x6B, 0x00)) return false; // PWR_MGMT_1: wake up
  writeReg(0x1C, 0x00);                    // accel range +-2g
  writeReg(0x1A, 0x03);                    // on-chip low-pass filter ~44 Hz
  return true;
}

void calibrate() {
  Bridge.notify("status", "Calibrating: hold head STILL and NEUTRAL");
  delay(500);
  float sr = 0, sp = 0; int n = 0;
  for (int i = 0; i < 50; i++) {
    float r, p;
    if (readAngles(r, p)) { sr += r; sp += p; n++; }
    delay(20);
  }
  if (n == 0) { sensorOk = false; Bridge.notify("status", "!! Calibration failed: no sensor data. Check wiring."); return; }
  roll0 = roll = sr / n;
  pitch0 = pitch = sp / n;
  armed = true; pending = NONE;
  sensorOk = true;
  String msg = String("Neutral set: roll=") + String(roll0, 1) + " pitch=" + String(pitch0, 1);
  Bridge.notify("status", msg);
}

void fire(Gesture g) {
  counts[g]++;
  Bridge.notify("gesture", NAMES[g]);
}

// Called from Linux (Python) over the Bridge; the actual work happens in loop().
void request_calibrate() { calibrateRequested = true; }

// ---------------------------------------------------------------------------
void setup() {
  Bridge.begin();
  Bridge.provide("calibrate", request_calibrate);
  delay(200);
  if (initSensor()) {
    Bridge.notify("status", bus == &Wire ? "GY-521 found on Wire (header SDA/SCL)" : "GY-521 found on Wire1 (Qwiic)");
    calibrate();
  } else {
    Bridge.notify("status", "!! No response from GY-521 at 0x68 on Wire or Wire1. Check VCC(3.3V)/GND/SDA/SCL.");
  }
}

void loop() {
  unsigned long now = millis();

  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    Bridge.notify("heartbeat", sensorOk);
  }

  if (calibrateRequested) {
    calibrateRequested = false;
    if (!sensorOk) initSensor();           // re-wake the sensor if it dropped out
    calibrate();
  }

  if (now - lastSample < SAMPLE_MS) return;
  lastSample = now;

  float r, p;
  if (!readAngles(r, p)) {
    sensorOk = false;
    if (now - lastScan > 3000) {
      lastScan = now;
      String msg = String("!! I2C scan: Wire(header SDA/SCL):") + scan(&Wire) + "  Wire1(Qwiic):" + scan(&Wire1);
      Bridge.notify("status", msg);
    }
    if (now - lastErr > 1000) {
      Bridge.notify("status", sameCount >= FROZEN_SAMPLES ? "!! Sensor data frozen (power dip?). Re-waking."
                                                          : "!! Sensor read failed. Check wiring.");
      sameCount = 0;
      lastErr = now;
      if (initSensor()) calibrate();       // try to recover (re-seated wire, power glitch)
    }
    return;
  }
  if (!sensorOk) {                       // back after a failure (re-seated wire / power glitch):
    if (initSensor()) calibrate();       // the MPU-6050 wakes up asleep, so wake it and re-zero
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
    if (cand != NONE) {
      if (cand != pending) { pending = cand; pendingSince = now; }
      else if (now - pendingSince >= HOLD_MS) { fire(cand); armed = false; }
    } else {
      pending = NONE;
    }
  } else if (fabs(dr) < TILT_OFF && fabs(dp) < TILT_OFF) {
    armed = true; pending = NONE;        // back to neutral: ready for next gesture
  }
}
