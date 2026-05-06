#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WebServer.h>
#include <esp_camera.h>

namespace {

// WLAN-Zugangsdaten und Ziel-URL des Raspberry-Pi-Verifikationsdiensts.
const char kWifiSsid[] = "hs-iot";
const char kWifiPassword[] = "hsiot2026";
const char kVerifierUrl[] = "http://10.42.0.1:8000/api/verify?person_id=jonathan";
const char kRingCaptureBaseUrl[] = "http://10.42.0.1:8000/api/ring-capture?person_id=jonathan";
const char kLogUrl[] = "http://10.42.0.1:8000/api/esp-log";

// HTTP-Server auf Port 80 für die Geräte-API.
WebServer server(80);

// Kameraeinstellungen: kleine Aufloesung fuer schnelle Einzelbilder.
const framesize_t kFrameSize = FRAMESIZE_QVGA;
const int kJpegQuality = 12;
const int kFrameBufferCount = 1;
const int kFlashLedPin = 4;
const int kButtonPin = 13;
const int kAccessLedRedPin = 15;
const int kAccessLedGreenPin = 2;
const int kAccessLedRedChannel = 2;
const int kAccessLedGreenChannel = 3;
const int kAccessLedPwmFrequency = 5000;
const int kAccessLedPwmResolution = 8;
const int kAccessLedMaxBrightness = 255;
const int kAccessLedVerifyingRedBrightness = 255;
const int kAccessLedVerifyingGreenBrightness = 70;
const int kBurstImageCount = 3;
const unsigned long kRingSettleDelayMs = 700;
const unsigned long kFlashWarmupDelayMs = 120;
const unsigned long kBurstPauseMs = 180;
const unsigned long kDiscardedFramePauseMs = 60;
const unsigned long kButtonDebounceMs = 60;
const unsigned long kRemoteLogRetryMs = 5000;
const int kRemoteLogQueueSize = 24;
const int kDiscardedFrameCount = 2;
const int kRequiredMatchesForAccess = 2;

bool gRingInProgress = false;
bool gLastButtonReading = HIGH;
bool gStableButtonState = HIGH;
unsigned long gLastButtonChangeMs = 0;
String gRemoteLogQueue[kRemoteLogQueueSize];
int gRemoteLogHead = 0;
int gRemoteLogCount = 0;
String gRemoteLogLine;
bool gRemoteLogSending = false;
unsigned long gLastRemoteLogAttemptMs = 0;

bool ensureWifiConnected();

void queueRemoteLogLine(const String &line) {
  if (line.length() == 0) {
    return;
  }

  int index = (gRemoteLogHead + gRemoteLogCount) % kRemoteLogQueueSize;
  if (gRemoteLogCount == kRemoteLogQueueSize) {
    gRemoteLogHead = (gRemoteLogHead + 1) % kRemoteLogQueueSize;
    gRemoteLogCount--;
  }

  gRemoteLogQueue[index] = line;
  gRemoteLogCount++;
}

void appendRemoteLogText(const String &text) {
  for (int i = 0; i < text.length(); ++i) {
    char c = text[i];
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      queueRemoteLogLine(gRemoteLogLine);
      gRemoteLogLine = "";
      continue;
    }

    gRemoteLogLine += c;
    if (gRemoteLogLine.length() >= 220) {
      queueRemoteLogLine(gRemoteLogLine);
      gRemoteLogLine = "";
    }
  }
}

void flushRemoteLogs() {
  if (gRemoteLogSending || WiFi.status() != WL_CONNECTED) {
    return;
  }
  if (gRemoteLogCount == 0) {
    return;
  }

  unsigned long now = millis();
  if (gLastRemoteLogAttemptMs != 0 && now - gLastRemoteLogAttemptMs < kRemoteLogRetryMs) {
    return;
  }

  gRemoteLogSending = true;
  while (gRemoteLogCount > 0 && WiFi.status() == WL_CONNECTED) {
    String line = gRemoteLogQueue[gRemoteLogHead];
    HTTPClient http;
    http.setTimeout(2500);

    bool sent = false;
    gLastRemoteLogAttemptMs = millis();
    if (http.begin(kLogUrl)) {
      http.addHeader("Content-Type", "text/plain; charset=utf-8");
      http.addHeader("X-ESP-MAC", WiFi.macAddress());
      int statusCode = http.POST(line);
      sent = statusCode > 0 && statusCode < 500;
      http.end();
    }

    if (!sent) {
      break;
    }

    gLastRemoteLogAttemptMs = 0;

    gRemoteLogQueue[gRemoteLogHead] = "";
    gRemoteLogHead = (gRemoteLogHead + 1) % kRemoteLogQueueSize;
    gRemoteLogCount--;
  }
  gRemoteLogSending = false;
}

void logPrint(const String &text) {
  Serial.print(text);
  appendRemoteLogText(text);
}

void logPrintln(const String &text = "") {
  Serial.println(text);
  appendRemoteLogText(text);
  appendRemoteLogText("\n");
}

void logPrintf(const char *format, ...) {
  char buffer[512];
  va_list args;
  va_start(args, format);
  vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  Serial.print(buffer);
  appendRemoteLogText(buffer);
}

void setFlashLed(bool enabled) {
  digitalWrite(kFlashLedPin, enabled ? HIGH : LOW);
}

void setAccessLedBrightness(int redBrightness, int greenBrightness) {
  ledcWrite(kAccessLedRedChannel, constrain(redBrightness, 0, kAccessLedMaxBrightness));
  ledcWrite(kAccessLedGreenChannel, constrain(greenBrightness, 0, kAccessLedMaxBrightness));
}

void setAccessLed(bool redEnabled, bool greenEnabled) {
  setAccessLedBrightness(
      redEnabled ? kAccessLedMaxBrightness : 0,
      greenEnabled ? kAccessLedMaxBrightness : 0);
}

void setAccessLedVerifying() {
  setAccessLedBrightness(kAccessLedVerifyingRedBrightness, kAccessLedVerifyingGreenBrightness);
}

void initAccessLed() {
  ledcSetup(kAccessLedRedChannel, kAccessLedPwmFrequency, kAccessLedPwmResolution);
  ledcSetup(kAccessLedGreenChannel, kAccessLedPwmFrequency, kAccessLedPwmResolution);
  ledcAttachPin(kAccessLedRedPin, kAccessLedRedChannel);
  ledcAttachPin(kAccessLedGreenPin, kAccessLedGreenChannel);
  setAccessLed(false, false);
}

void showVerificationResult(bool admitted) {
  setAccessLed(!admitted, admitted);
}

void blinkFlashLed(int blinkCount, unsigned long onMs, unsigned long offMs) {
  for (int i = 0; i < blinkCount; ++i) {
    setFlashLed(true);
    delay(onMs);
    setFlashLed(false);
    if (i < blinkCount - 1) {
      delay(offMs);
    }
  }
}

void blinkStartSequence() {
  blinkFlashLed(2, 220, 260);
}

bool jsonHasTrue(const String &payload, const char *key) {
  String pattern = String("\"") + key + "\":true";
  return payload.indexOf(pattern) >= 0;
}

bool jsonHasFalse(const String &payload, const char *key) {
  String pattern = String("\"") + key + "\":false";
  return payload.indexOf(pattern) >= 0;
}

bool jsonHasKey(const String &payload, const char *key) {
  String pattern = String("\"") + key + "\":";
  return payload.indexOf(pattern) >= 0;
}

bool responseAllowsAccess(const String &payload) {
  if (jsonHasTrue(payload, "overall_matched")) {
    return true;
  }
  if (jsonHasFalse(payload, "overall_matched")) {
    return false;
  }
  return jsonHasTrue(payload, "matched");
}

bool responseHasVerificationResult(const String &payload) {
  return jsonHasKey(payload, "overall_matched") || jsonHasKey(payload, "matched");
}

void blinkAccessError() {
  for (int i = 0; i < 3; ++i) {
    setAccessLed(true, false);
    delay(120);
    setAccessLed(false, false);
    delay(120);
  }
}

void showAccessLedForResponse(const String &payload) {
  if (!responseHasVerificationResult(payload)) {
    logPrintln("[ACCESS] Kein Verifikationsergebnis, LED bleibt aus.");
    blinkAccessError();
    return;
  }

  bool admitted = responseAllowsAccess(payload);
  showVerificationResult(admitted);
  logPrintf("[ACCESS] Ergebnis: %s\n", admitted ? "Zulassung" : "Ablehnung");
}

int jsonGetInt(const String &payload, const char *key, int fallback = -1) {
  String pattern = String("\"") + key + "\":";
  int start = payload.indexOf(pattern);
  if (start < 0) {
    return fallback;
  }
  start += pattern.length();
  while (start < payload.length() && payload[start] == ' ') {
    ++start;
  }
  int end = start;
  while (end < payload.length() && (isDigit(payload[end]) || payload[end] == '-')) {
    ++end;
  }
  if (end == start) {
    return fallback;
  }
  return payload.substring(start, end).toInt();
}

camera_fb_t *captureFrameWithFlash() {
  setFlashLed(true);
  delay(kFlashWarmupDelayMs);

  for (int i = 0; i < kDiscardedFrameCount; ++i) {
    camera_fb_t *staleFrame = esp_camera_fb_get();
    if (staleFrame != nullptr) {
      esp_camera_fb_return(staleFrame);
      logPrintf("[CAM] Verwerfe altes Frame %d/%d.\n", i + 1, kDiscardedFrameCount);
    }
    delay(kDiscardedFramePauseMs);
  }

  camera_fb_t *frame = esp_camera_fb_get();
  setFlashLed(false);
  return frame;
}

String postFrameToPi(
    const String &url,
    camera_fb_t *frame,
    int *statusCode,
    bool *captureMatched,
    bool *overallMatched,
    int *eventId) {
  HTTPClient http;
  http.setTimeout(15000);

  String responseBody = "{\"ok\":false,\"error\":\"Unbekannter Fehler.\"}";
  *statusCode = -1;
  *captureMatched = false;
  *overallMatched = false;

  logPrintf("[RING] Sende Bild an %s\n", url.c_str());
  if (!http.begin(url)) {
    logPrintln("[RING] Verifier-URL konnte nicht initialisiert werden.");
    return "{\"ok\":false,\"error\":\"Verifier-URL konnte nicht initialisiert werden.\"}";
  }

  http.addHeader("Content-Type", "image/jpeg");
  *statusCode = http.POST(frame->buf, frame->len);
  logPrintf("[RING] POST abgeschlossen. Status: %d\n", *statusCode);

  if (*statusCode > 0) {
    responseBody = http.getString();
    logPrintf("[RING] Antwort vom Pi: %s\n", responseBody.c_str());
    *captureMatched = jsonHasTrue(responseBody, "matched");
    *overallMatched = jsonHasTrue(responseBody, "overall_matched");
    if (eventId != nullptr) {
      int parsedEventId = jsonGetInt(responseBody, "event_id", -1);
      if (parsedEventId >= 0) {
        *eventId = parsedEventId;
      }
    }
  } else {
    responseBody = String("{\"ok\":false,\"error\":\"HTTP POST fehlgeschlagen: ") + http.errorToString(*statusCode) + "\"}";
    logPrintf("[RING] HTTP-Fehler: %s\n", http.errorToString(*statusCode).c_str());
  }

  http.end();
  return responseBody;
}

String runRingWorkflow() {
  if (gRingInProgress) {
    return "{\"ok\":false,\"error\":\"Klingelworkflow laeuft bereits.\"}";
  }

  gRingInProgress = true;
  logPrintln("[RING] Klingelereignis gestartet.");

  if (!ensureWifiConnected()) {
    gRingInProgress = false;
    logPrintln("[RING] WLAN nicht verbunden.");
    return "{\"ok\":false,\"error\":\"ESP ist nicht mit dem WLAN verbunden.\"}";
  }

  logPrintln("[RING] Startsignal blinkt.");
  blinkStartSequence();
  delay(kRingSettleDelayMs);

  int matchedImages = 0;
  bool allSuccessful = true;
  int eventId = -1;
  String lastResponse = "{\"ok\":false,\"error\":\"Keine Antwort vom Pi erhalten.\"}";

  for (int sequence = 1; sequence <= kBurstImageCount; ++sequence) {
    logPrintf("[RING] Erfasse Bild %d/%d ...\n", sequence, kBurstImageCount);
    camera_fb_t *frame = captureFrameWithFlash();
    if (frame == nullptr) {
      logPrintln("[RING] Kamerabild konnte nicht gelesen werden.");
      allSuccessful = false;
      lastResponse = "{\"ok\":false,\"error\":\"Kamerabild konnte nicht gelesen werden.\"}";
      continue;
    }

    logPrintf("[RING] Bild %d vorhanden. Groesse: %u Bytes\n", sequence, frame->len);

    String url = String(kRingCaptureBaseUrl) +
                 "&sequence=" + sequence +
                 "&total=" + kBurstImageCount;
    if (eventId >= 0) {
      url += "&event_id=" + String(eventId);
    }

    int statusCode = -1;
    bool captureMatched = false;
    bool overallMatched = false;
    lastResponse = postFrameToPi(url, frame, &statusCode, &captureMatched, &overallMatched, &eventId);
    esp_camera_fb_return(frame);

    if (statusCode <= 0) {
      allSuccessful = false;
    }

    if (captureMatched) {
      matchedImages++;
    }
    delay(kBurstPauseMs);
  }

  gRingInProgress = false;
  bool accessGranted = matchedImages >= kRequiredMatchesForAccess;

  if (!allSuccessful && matchedImages == 0) {
    return lastResponse;
  }

  String summary = String("{\"ok\":true,\"event_id\":") + eventId +
                   ",\"matched_images\":" + matchedImages +
                   ",\"required_matches\":" + kRequiredMatchesForAccess +
                   ",\"overall_matched\":" + (accessGranted ? "true" : "false") +
                   ",\"last_response\":" + lastResponse + "}";
  logPrintf("[RING] Klingelereignis abgeschlossen: %s\n", summary.c_str());
  return summary;
}

String runRingWorkflowWithAccessLed() {
  setAccessLedVerifying();
  String responseBody = runRingWorkflow();
  showAccessLedForResponse(responseBody);
  return responseBody;
}

bool initCamera() {
  bool hasPsram = psramFound();
  logPrintf("[CAM] PSRAM: %s\n", hasPsram ? "gefunden" : "nicht gefunden");
  logPrintln("[CAM] Initialisiere Kamera...");

  // Das Pinout ist hier fuer das gaengige ESP32-CAM / AI-Thinker-Layout gesetzt.
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 5;
  config.pin_d1 = 18;
  config.pin_d2 = 19;
  config.pin_d3 = 21;
  config.pin_d4 = 36;
  config.pin_d5 = 39;
  config.pin_d6 = 34;
  config.pin_d7 = 35;
  config.pin_xclk = 0;
  config.pin_pclk = 22;
  config.pin_vsync = 25;
  config.pin_href = 23;
  config.pin_sccb_sda = 26;
  config.pin_sccb_scl = 27;
  config.pin_pwdn = 32;
  config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = hasPsram ? kFrameSize : FRAMESIZE_QQVGA;
  config.jpeg_quality = hasPsram ? kJpegQuality : 15;
  config.fb_count = kFrameBufferCount;
  config.grab_mode = CAMERA_GRAB_LATEST;
#if defined(CAMERA_FB_IN_PSRAM) && defined(CAMERA_FB_IN_DRAM)
  config.fb_location = hasPsram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
#endif

  // Kamera-Hardware initialisieren.
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    logPrintf("Kamera-Start fehlgeschlagen. Fehlercode: 0x%x\n", err);
    return false;
  }
  logPrintln("[CAM] Kamera bereit.");

  // Kleine Bildoptimierungen fuer das angezeigte Kamerabild.
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor != nullptr) {
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, -1);
  }

  return true;
}

bool connectToWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(kWifiSsid, kWifiPassword);

  logPrintln();
  logPrint("Verbinde mit WLAN");

  const unsigned long startMillis = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startMillis < 20000) {
    delay(500);
    logPrint(".");
  }
  logPrintln();

  if (WiFi.status() != WL_CONNECTED) {
    logPrintln("WLAN-Verbindung fehlgeschlagen.");
    return false;
  }

  logPrint("WLAN verbunden. ESP-IP: ");
  logPrintln(WiFi.localIP().toString());
  logPrint("Verifier-Ziel: ");
  logPrintln(kVerifierUrl);
  flushRemoteLogs();
  return true;
}

bool ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  logPrintln("[WIFI] Nicht verbunden, versuche erneute Verbindung.");
  WiFi.disconnect();
  return connectToWifi();
}

void handleStatus() {
  String body = String("{\"ok\":true,\"ip\":\"") + WiFi.localIP().toString() +
                "\",\"ring_in_progress\":" + (gRingInProgress ? "true" : "false") + "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void handleSnapshot() {
  camera_fb_t *frame = captureFrameWithFlash();
  if (frame == nullptr) {
    logPrintln("[SNAPSHOT] Kamerabild konnte nicht gelesen werden.");
    server.send(500, "text/plain; charset=utf-8", "Kamerabild konnte nicht gelesen werden.");
    return;
  }

  server.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  server.send_P(200, "image/jpeg", reinterpret_cast<const char *>(frame->buf), frame->len);
  esp_camera_fb_return(frame);
}

void handleVerify() {
  logPrintln("[VERIFY] Anfrage vom Browser empfangen.");
  setAccessLedVerifying();

  if (!ensureWifiConnected()) {
    logPrintln("[VERIFY] WLAN nicht verbunden.");
    blinkAccessError();
    server.send(503, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"ESP ist nicht mit dem WLAN verbunden.\"}");
    return;
  }

  logPrintln("[VERIFY] Hole Kamerabild...");
  camera_fb_t *frame = captureFrameWithFlash();
  if (frame == nullptr) {
    logPrintln("[VERIFY] Kamerabild konnte nicht gelesen werden.");
    blinkAccessError();
    server.send(500, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"Kamerabild konnte nicht gelesen werden.\"}");
    return;
  }

  logPrintf("[VERIFY] Kamerabild vorhanden. Groesse: %u Bytes\n", frame->len);

  HTTPClient http;
  http.setTimeout(15000);

  int statusCode = -1;
  String responseBody = "{\"ok\":false,\"error\":\"Unbekannter Fehler.\"}";

  logPrintf("[VERIFY] Sende POST an %s\n", kVerifierUrl);
  if (http.begin(kVerifierUrl)) {
    http.addHeader("Content-Type", "image/jpeg");
    statusCode = http.POST(frame->buf, frame->len);
    logPrintf("[VERIFY] POST abgeschlossen. Status: %d\n", statusCode);

    if (statusCode > 0) {
      responseBody = http.getString();
      logPrintf("[VERIFY] Antwort vom Pi: %s\n", responseBody.c_str());
    } else {
      logPrintf("[VERIFY] HTTP-Fehler: %s\n", http.errorToString(statusCode).c_str());
      responseBody = String("{\"ok\":false,\"error\":\"HTTP POST fehlgeschlagen: ") + http.errorToString(statusCode) + "\"}";
    }

    http.end();
  } else {
    logPrintln("[VERIFY] Verifier-URL konnte nicht initialisiert werden.");
    responseBody = "{\"ok\":false,\"error\":\"Verifier-URL konnte nicht initialisiert werden.\"}";
  }

  esp_camera_fb_return(frame);
  logPrintln("[VERIFY] Kamerabild freigegeben, sende Antwort an Browser.");

  showAccessLedForResponse(responseBody);
  server.send(statusCode > 0 ? statusCode : 502, "application/json; charset=utf-8", responseBody);
  logPrintln("[VERIFY] Browser-Antwort gesendet.");
}

void handleRingRequest() {
  String responseBody = runRingWorkflowWithAccessLed();
  bool ok = jsonHasTrue(responseBody, "ok");
  server.send(ok ? 200 : 500, "application/json; charset=utf-8", responseBody);
}

void handleButton() {
  bool reading = digitalRead(kButtonPin);
  unsigned long now = millis();

  if (reading != gLastButtonReading) {
    gLastButtonChangeMs = now;
    gLastButtonReading = reading;
  }

  if (now - gLastButtonChangeMs < kButtonDebounceMs || reading == gStableButtonState) {
    return;
  }

  gStableButtonState = reading;
  if (gStableButtonState == LOW) {
    logPrintln("[BUTTON] Taster gedrueckt, starte Verifikation.");
    runRingWorkflowWithAccessLed();
  }
}

void startServer() {
  // Geraete-API: Snapshot, Status und Ring-Trigger.
  server.on("/", HTTP_GET, handleStatus);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/snapshot", HTTP_GET, handleSnapshot);
  server.on("/verify", HTTP_POST, handleVerify);
  server.on("/ring", HTTP_POST, handleRingRequest);
  server.begin();
  logPrintln("Webserver gestartet.");
}

}  // namespace

void setup() {
  // Serielle Ausgabe zum Debuggen starten.
  Serial.begin(115200);
  delay(1500);
  logPrintln();
  logPrintln("ESP32-CAM Live-Test startet...");

  pinMode(kFlashLedPin, OUTPUT);
  pinMode(kButtonPin, INPUT_PULLUP);
  setFlashLed(false);
  initAccessLed();

  // Ohne funktionierende Kamera lohnt es sich nicht, WLAN und Server zu starten.
  if (!initCamera()) {
    logPrintln("Bitte Pinout und Stromversorgung pruefen, dann Reset druecken.");
    return;
  }

  // Danach Netzwerk und Webserver einschalten.
  connectToWifi();
  startServer();
  logPrintf("[BUTTON] Bereit. Taster an GPIO%d gegen GND startet die Verifikation.\n", kButtonPin);
}

void loop() {
  // Eingehende Browser-Anfragen laufend bearbeiten.
  server.handleClient();
  handleButton();
  flushRemoteLogs();
}
