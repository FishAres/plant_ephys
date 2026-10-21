/*
 * plant_sensor_v2.ino  -  Plant electrophysiology recorder (corrected design)
 *
 * Hardware: INA128 (G = 6, Rg = 10k) -> ADS1115 -> Arduino Uno R4 (I2C)
 *   INA128 pin 6 (Vout) -> 10k -> ADS1115 A0   (100 nF from A0 to GND)
 *   INA128 pin 5 (REF)  -> 10k -> ADS1115 A1   (100 nF from A1 to GND)
 *   The ADC reads A0 - A1, so the REF voltage (and any 5 V supply noise
 *   riding on it) cancels exactly.
 *
 * Output: CSV over Serial at 115200 baud
 *   time_ms,raw_code,plant_uV
 *
 * Serial commands (send one character):
 *   s  start/stop streaming (starts streaming on boot)
 *   n  10-second noise/offset statistics (short +IN to -IN first)
 *   c  check node voltages (A0, A1 single-ended; expect ~2.2-2.6 V each)
 */

#include <Wire.h>
#include <Adafruit_ADS1X15.h>

Adafruit_ADS1115 ads;

const float INA_GAIN = 6.0;          // 1 + 50k / Rg, Rg = 10k
// GAIN_TWO = +/-2.048 V at the ADC -> +/-341 mV at the electrodes,
// 62.5 uV/LSB at the ADC -> 10.4 uV/LSB at the electrodes.
// For smaller signals try GAIN_FOUR (+/-170 mV, 5.2 uV/LSB).
const adsGain_t ADC_GAIN = GAIN_TWO;

bool streaming = true;

float readPlantMicrovolts(int16_t &raw) {
  raw = ads.readADC_Differential_0_1();          // A0 - A1
  return ads.computeVolts(raw) * 1e6 / INA_GAIN; // volts at ADC -> uV at plant
}

void checkNodes() {
  ads.setGain(GAIN_ONE);                          // +/-4.096 V for absolute reads
  float a0 = ads.computeVolts(ads.readADC_SingleEnded(0));
  float a1 = ads.computeVolts(ads.readADC_SingleEnded(1));
  ads.setGain(ADC_GAIN);
  Serial.print(F("# A0 = ")); Serial.print(a0, 4);
  Serial.print(F(" V, A1 (REF) = ")); Serial.print(a1, 4);
  Serial.print(F(" V, A0-A1 = ")); Serial.print((a0 - a1) * 1000, 2);
  Serial.println(F(" mV"));
  if (a1 < 1.5 || a1 > 3.0)
    Serial.println(F("# WARNING: REF not near 2.5 V - check 10k/10k divider and pin 5"));
  if (a0 < 0.3 || a0 > 4.7)
    Serial.println(F("# WARNING: INA128 output near a rail - check inputs, ground electrode, +/-9 V"));
}

void noiseStats() {
  Serial.println(F("# Measuring 10 s ..."));
  double sum = 0, sum2 = 0, mn = 1e12, mx = -1e12;
  int n = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < 10000) {
    int16_t raw;
    float uv = readPlantMicrovolts(raw);
    sum += uv; sum2 += (double)uv * uv; n++;
    if (uv < mn) mn = uv;
    if (uv > mx) mx = uv;
  }
  double mean = sum / n, sd = sqrt(max(0.0, sum2 / n - mean * mean));
  Serial.print(F("# n=")); Serial.print(n);
  Serial.print(F("  mean=")); Serial.print(mean, 1);
  Serial.print(F(" uV  rms_noise=")); Serial.print(sd, 1);
  Serial.print(F(" uV  p-p=")); Serial.print(mx - mn, 1);
  Serial.println(F(" uV"));
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  Wire.begin();
  if (!ads.begin(0x48)) {   // ADDR pin to GND
    Serial.println(F("# ERROR: ADS1115 not found (check VDD, GND, SDA->A4, SCL->A5, ADDR->GND)"));
    while (true) delay(1000);
  }
  ads.setGain(ADC_GAIN);
  ads.setDataRate(RATE_ADS1115_8SPS);   // 8 SPS: lowest noise, ample for plant signals
  Serial.println(F("# plant_sensor_v2  (commands: s=stream on/off, n=noise stats, c=check nodes)"));
  checkNodes();
  Serial.println(F("time_ms,raw_code,plant_uV"));
}

void loop() {
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 's') streaming = !streaming;
    else if (c == 'n') noiseStats();
    else if (c == 'c') checkNodes();
  }
  if (streaming) {
    int16_t raw;
    float uv = readPlantMicrovolts(raw);   // blocks ~125 ms at 8 SPS
    Serial.print(millis()); Serial.print(',');
    Serial.print(raw);      Serial.print(',');
    Serial.println(uv, 1);
  }
}
