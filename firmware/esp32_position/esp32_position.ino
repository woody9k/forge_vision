// Forge Vision position reporter for ESP32
// ---------------------------------------------------------------------------
// Streams one JSON line per update over USB serial, which Forge Vision reads
// as a SerialSource. Only distance is required; everything else is optional
// and simply omitted when the sensor is absent — the platform reports a
// missing field as unknown rather than inventing a value.
//
//   {"t":12.345,"counts":1830,"x_m":1.372,"heading_deg":91.2}
//
// WIRING — quadrature survey wheel (the usual case)
//   Encoder A  -> GPIO 32
//   Encoder B  -> GPIO 33
//   Encoder 5V -> 5V (or 3V3 for a 3.3 V encoder)
//   Encoder GND-> GND
// A "survey wheel" is just a wheel of known circumference with a rotary
// encoder on its axle: roll it along the scan line and the counts tell you
// exactly how far the antenna has travelled. Set WHEEL_CIRCUMFERENCE_M and
// COUNTS_PER_REV below and the maths is done on the host.
//
// A single-channel sensor (one hall effect + magnet, or an optical slot)
// works too — set QUADRATURE to 0. You lose direction sensing, so the count
// only ever increases: fine for a one-way scan line, wrong if you roll back.
//
// OPTIONAL IMU (heading/tilt). An MPU-6050 or BNO055 on I2C:
//   SDA -> GPIO 21, SCL -> GPIO 22
// Enable with USE_IMU and fill in readImu(). Orientation matters because a
// tilted antenna changes the geometry the image is reconstructed under.
//
// A NOTE ON GPS: consumer GPS is metres-accurate, and scan steps here are
// centimetres. Do not use GPS as the scan-line position. It is useful for
// tagging which site you are at, and its pulse-per-second output is an
// excellent clock — but the wheel is what measures the line.
// ---------------------------------------------------------------------------

#include <Arduino.h>

// ---- configuration --------------------------------------------------------
static const int   PIN_ENCODER_A = 32;
static const int   PIN_ENCODER_B = 33;
static const bool  QUADRATURE    = true;   // false for a single-channel sensor
static const int   REPORT_HZ     = 20;     // position updates per second
static const bool  USE_IMU       = false;  // set true once an IMU is wired

// Measure the wheel, do not trust the moulding. Roll it ten turns along a
// tape measure and divide: a 2 % circumference error is a 2 % scale error in
// every B-scan you produce.
static const float WHEEL_CIRCUMFERENCE_M = 0.3141593f;  // 100 mm dia wheel
static const long  COUNTS_PER_REV        = 2400;        // 600 PPR x4 quadrature

// ---- encoder state --------------------------------------------------------
volatile long  encoderCounts = 0;
volatile uint8_t lastState   = 0;

// Quadrature decode: the A/B phase pair tells you direction as well as motion.
// The table maps (previous state, current state) to -1, 0 or +1.
static const int8_t QUAD_TABLE[16] = {
   0, -1,  1,  0,
   1,  0,  0, -1,
  -1,  0,  0,  1,
   0,  1, -1,  0
};

void IRAM_ATTR onEncoderEdge() {
  uint8_t a = digitalRead(PIN_ENCODER_A);
  uint8_t b = digitalRead(PIN_ENCODER_B);
  uint8_t state = (a << 1) | b;
  if (QUADRATURE) {
    encoderCounts += QUAD_TABLE[(lastState << 2) | state];
  } else {
    encoderCounts++;                 // no direction information available
  }
  lastState = state;
}

// ---- optional IMU ---------------------------------------------------------
bool readImu(float &headingDeg, float &pitchDeg, float &rollDeg) {
  // Fill in for your part (MPU-6050, BNO055, ...). Returning false simply
  // omits the fields, which the host records as "not measured".
  (void)headingDeg; (void)pitchDeg; (void)rollDeg;
  return false;
}

// ---- setup / loop ---------------------------------------------------------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_ENCODER_A, INPUT_PULLUP);
  pinMode(PIN_ENCODER_B, INPUT_PULLUP);
  lastState = (digitalRead(PIN_ENCODER_A) << 1) | digitalRead(PIN_ENCODER_B);
  attachInterrupt(digitalPinToInterrupt(PIN_ENCODER_A), onEncoderEdge, CHANGE);
  if (QUADRATURE) {
    attachInterrupt(digitalPinToInterrupt(PIN_ENCODER_B), onEncoderEdge, CHANGE);
  }
  // Comment lines are ignored by the host, so they are safe for banners.
  Serial.println("# forge-vision position reporter v1");
  Serial.printf("# wheel_circumference_m=%.6f counts_per_rev=%ld\n",
                WHEEL_CIRCUMFERENCE_M, COUNTS_PER_REV);
}

void loop() {
  static uint32_t nextReport = 0;
  uint32_t now = millis();
  if (now < nextReport) return;
  nextReport = now + (1000 / REPORT_HZ);

  noInterrupts();
  long counts = encoderCounts;
  interrupts();

  float metres = (float)counts * WHEEL_CIRCUMFERENCE_M / (float)COUNTS_PER_REV;

  Serial.print("{\"t\":");        Serial.print(now / 1000.0f, 3);
  Serial.print(",\"counts\":");   Serial.print(counts);
  Serial.print(",\"x_m\":");      Serial.print(metres, 4);

  if (USE_IMU) {
    float h, p, r;
    if (readImu(h, p, r)) {
      Serial.print(",\"heading_deg\":"); Serial.print(h, 1);
      Serial.print(",\"pitch_deg\":");   Serial.print(p, 1);
      Serial.print(",\"roll_deg\":");    Serial.print(r, 1);
    }
  }
  Serial.println("}");

  // Send "z" over serial to zero the wheel at the start of a scan line.
  while (Serial.available()) {
    if (Serial.read() == 'z') {
      noInterrupts();
      encoderCounts = 0;
      interrupts();
      Serial.println("# zeroed");
    }
  }
}
