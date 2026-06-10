#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <esp_camera.h>
#include <esp_sleep.h>

// WLAN-Zugangsdaten werden aus einer lokalen, nicht versionierten Datei geladen
// (Vorlage: include/secrets.example.h). Fehlt secrets.h, bricht der Build bewusst
// ab, statt dass fehlende Zugangsdaten erst zur Laufzeit auf dem ESP auffallen.
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "secrets.h fehlt. Kopiere include/secrets.example.h zu include/secrets.h und trage die WLAN-Zugangsdaten ein."
#endif

namespace {

// Ziel-URLs des Raspberry-Pi-Verifikationsdiensts. Die WLAN-Zugangsdaten kommen
// aus secrets.h (WIFI_SSID / WIFI_PASSWORD) und stehen nicht mehr im Quellcode.
const char kWifiSsid[] = WIFI_SSID;
const char kWifiPassword[] = WIFI_PASSWORD;
const char kVerifierUrl[] = "http://10.42.0.1:8000/api/verify";
const char kRingCaptureBaseUrl[] = "http://10.42.0.1:8000/api/ring-capture";
const char kLogUrl[] = "http://10.42.0.1:8000/api/esp-log";
const char kRingDecisionBaseUrl[] = "http://10.42.0.1:8000/api/ring-decision";

// Feste IP-Konfiguration: spart beim Verbinden den DHCP-Handshake. Die Adresse
// muss zur Pi-Config passen (esp_snapshot_url -> 10.42.0.172). Gateway/DNS ist
// der Pi-Hotspot (10.42.0.1).
const IPAddress kStaticIp(10, 42, 0, 172);
const IPAddress kGateway(10, 42, 0, 1);
const IPAddress kSubnet(255, 255, 255, 0);
const IPAddress kPrimaryDns(10, 42, 0, 1);

// HTTP-Server auf Port 80 für die Geräte-API.
WebServer server(80);

// Kameraeinstellungen: kleine Aufloesung fuer schnelle Einzelbilder.
const framesize_t kFrameSize = FRAMESIZE_QVGA;
const int kJpegQuality = 12;
const int kFrameBufferCount = 1;
const int kFlashLedPin = 4;
const int kButtonPin = 12;
const int kI2cSdaPin = 13;
const int kI2cSclPin = 14;
const int kMotionSensorPin = 15;
const uint64_t kDeepSleepWakeupPinMask = (1ULL << GPIO_NUM_12) | (1ULL << GPIO_NUM_15);
const int kAccessLedGreenPin = 2;
const int kAccessLedGreenChannel = 3;
const int kAccessLedPwmFrequency = 5000;
const int kAccessLedPwmResolution = 8;
const int kAccessLedMaxBrightness = 255;
const int kAccessLedVerifyingGreenBrightness = 90;
const int kAccessLedWifiGreenBrightness = 70;
const int kBurstImageCount = 3;
const unsigned long kRingSettleDelayMs = 700;
const unsigned long kFlashWarmupDelayMs = 120;
const unsigned long kBurstPauseMs = 180;
const unsigned long kDiscardedFramePauseMs = 60;
const unsigned long kButtonDebounceMs = 60;
const unsigned long kRemoteLogRetryMs = 5000;
const unsigned long kDisplayRetryMs = 10000;
const unsigned long kMotionIdleBeforeSleepTimeoutMs = 30000;
const unsigned long kMotionIdlePollMs = 250;
const unsigned long kTelegramDecisionPollIntervalMs = 2000;
const unsigned long kTelegramDecisionTimeoutMs = 90000;
const unsigned long kIdleDeepSleepTimeoutMs = 90000;
const int kRemoteLogQueueSize = 24;
const int kDiscardedFrameCount = 2;
const int kRequiredMatchesForAccess = 2;
const uint8_t kOledAddress = 0x3C;
const int kOledWidth = 128;
const int kOledHeight = 64;

bool gRingInProgress = false;
bool gLastButtonReading = LOW;
bool gStableButtonState = LOW;
bool gLastMotionReading = LOW;
unsigned long gLastButtonChangeMs = 0;
String gRemoteLogQueue[kRemoteLogQueueSize];
int gRemoteLogHead = 0;
int gRemoteLogCount = 0;
String gRemoteLogLine;
bool gRemoteLogSending = false;
unsigned long gLastRemoteLogAttemptMs = 0;
unsigned long gLastUserActivityMs = 0;
bool gDisplayAvailable = false;
bool gDisplayInitAttempted = false;
unsigned long gLastDisplayInitAttemptMs = 0;
Adafruit_SSD1306 display(kOledWidth, kOledHeight, &Wire, -1);

bool ensureWifiConnected();
void logPrint(const String &text);
void logPrintln(const String &text = "");
void logPrintf(const char *format, ...);
int jsonGetInt(const String &payload, const char *key, int fallback = -1);
String jsonGetString(const String &payload, const char *key, const String &fallback = "");
void waitForTelegramDecision(int eventId);
void markUserActivity();

void markUserActivity() {
  gLastUserActivityMs = millis();
}

void logWakeupReason() {
  esp_sleep_wakeup_cause_t wakeupCause = esp_sleep_get_wakeup_cause();
  switch (wakeupCause) {
    case ESP_SLEEP_WAKEUP_EXT1: {
      uint64_t wakeupPins = esp_sleep_get_ext1_wakeup_status();
      logPrintf("[SLEEP] Aufgewacht durch EXT1. Wakeup-Pins: 0x%llx", wakeupPins);
      if ((wakeupPins & (1ULL << GPIO_NUM_12)) != 0) {
        logPrint(" GPIO12/Taster");
      }
      if ((wakeupPins & (1ULL << GPIO_NUM_15)) != 0) {
        logPrint(" GPIO15/Bewegung");
      }
      logPrintln();
      break;
    }
    case ESP_SLEEP_WAKEUP_EXT0:
      logPrintln("[SLEEP] Aufgewacht durch EXT0.");
      break;
    case ESP_SLEEP_WAKEUP_TIMER:
      logPrintln("[SLEEP] Aufgewacht durch Timer.");
      break;
    case ESP_SLEEP_WAKEUP_UNDEFINED:
      logPrintln("[SLEEP] Normaler Start, kein Deep-Sleep-Wakeup.");
      break;
    default:
      logPrintf("[SLEEP] Aufwachgrund: %d\n", static_cast<int>(wakeupCause));
      break;
  }
}

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

void logPrintln(const String &text) {
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

bool i2cDeviceResponds(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

void displayShowText(const String &message, uint8_t textSize = 1) {
  if (!gDisplayAvailable) {
    logPrintln("[OLED] Kein SSD1306-OLED verfuegbar.");
    return;
  }

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(textSize);
  display.setTextWrap(true);
  display.setCursor(0, 0);
  display.print(message);
  display.display();
  logPrintf("[OLED] Zeige Text: %s\n", message.c_str());
}

void displayShowWelcome() {
  displayShowText("Herzlich\nWillkommen", 1);
}

void displayTurnOff() {
  if (!gDisplayAvailable) {
    return;
  }

  display.clearDisplay();
  display.display();
  display.ssd1306_command(SSD1306_DISPLAYOFF);
  logPrintln("[OLED] Display ausgeschaltet.");
}

void initI2cDisplay() {
  gDisplayInitAttempted = true;
  gLastDisplayInitAttemptMs = millis();
  Wire.begin(kI2cSdaPin, kI2cSclPin);
  Wire.setClock(100000);
  Wire.setTimeOut(50);

  if (!i2cDeviceResponds(kOledAddress)) {
    gDisplayAvailable = false;
    logPrintln("[OLED] Kein SSD1306-OLED auf 0x3C gefunden.");
    return;
  }

  gDisplayAvailable = display.begin(SSD1306_SWITCHCAPVCC, kOledAddress);
  if (!gDisplayAvailable) {
    logPrintln("[OLED] SSD1306-Initialisierung fehlgeschlagen.");
    return;
  }

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setTextWrap(true);
  display.display();
  display.ssd1306_command(SSD1306_DISPLAYON);
  logPrintf("[OLED] SSD1306-OLED auf 0x%02x bereit. SDA GPIO%d, SCL GPIO%d.\n",
            kOledAddress,
            kI2cSdaPin,
            kI2cSclPin);
}

void ensureI2cDisplayReady() {
  if (gDisplayAvailable) {
    return;
  }
  if (gDisplayInitAttempted && millis() - gLastDisplayInitAttemptMs < kDisplayRetryMs) {
    return;
  }

  initI2cDisplay();
}

void setFlashLed(bool enabled) {
  digitalWrite(kFlashLedPin, enabled ? HIGH : LOW);
}

void setAccessLedBrightness(int greenBrightness) {
  ledcWrite(kAccessLedGreenChannel, constrain(greenBrightness, 0, kAccessLedMaxBrightness));
}

void setAccessLed(bool greenEnabled) {
  setAccessLedBrightness(greenEnabled ? kAccessLedMaxBrightness : 0);
}

void setAccessLedVerifying() {
  setAccessLedBrightness(kAccessLedVerifyingGreenBrightness);
}

void setAccessLedWifiConnecting(bool enabled) {
  setAccessLedBrightness(enabled ? kAccessLedWifiGreenBrightness : 0);
}

void initAccessLed() {
  ledcSetup(kAccessLedGreenChannel, kAccessLedPwmFrequency, kAccessLedPwmResolution);
  ledcAttachPin(kAccessLedGreenPin, kAccessLedGreenChannel);
  setAccessLed(false);
}

void showVerificationResult(bool admitted) {
  setAccessLed(admitted);
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
    setAccessLed(true);
    delay(120);
    setAccessLed(false);
    delay(120);
  }
}

void blinkVerificationOutcome(bool admitted) {
  const int blinkCount = admitted ? 5 : 12;
  const unsigned long onMs = admitted ? 450 : 100;
  const unsigned long offMs = admitted ? 450 : 100;

  for (int i = 0; i < blinkCount; ++i) {
    setAccessLed(true);
    delay(onMs);
    setAccessLed(false);
    delay(offMs);
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

void displayShowVerificationResult(const String &payload) {
  if (!responseHasVerificationResult(payload)) {
    displayShowText("Verifikation\nfehlgeschlagen", 1);
    return;
  }

  bool admitted = responseAllowsAccess(payload);
  bool faceDetectedInBurst = jsonHasTrue(payload, "face_detected");
  int detectedFaceImages = jsonGetInt(payload, "detected_face_images", -1);
  int detectedFaces = jsonGetInt(payload, "detected_faces", -1);
  int matchedImages = jsonGetInt(payload, "matched_images", -1);

  String message;
  if (detectedFaceImages == 0 || (!faceDetectedInBurst && detectedFaceImages < 0 && detectedFaces == 0)) {
    message = "Kein Gesicht\n";
  } else if (faceDetectedInBurst || detectedFaceImages > 0 || detectedFaces > 0) {
    message = "Gesicht erkannt\n";
  } else {
    message = "Gesicht geprueft\n";
  }

  message += admitted ? "Empfehlung:\nZulassen" : "Empfehlung:\nAblehnen";
  if (matchedImages >= 0) {
    message += "\nMatches: ";
    message += matchedImages;
    message += "/";
    message += kBurstImageCount;
  }

  displayShowText(message, 1);
}

void displayShowRemoteDecision(const String &decision) {
  if (decision == "approve") {
    displayShowText("Zulassung durch\nEigentuemer.", 1);
    return;
  }
  if (decision == "deny") {
    displayShowText("Ablehnung durch\nEigentuemer.", 1);
    return;
  }
  displayShowText("Eigentuemer wird\nbenachrichtigt\nWarte auf Antwort", 1);
}

void displayShowNoTelegramDecision() {
  displayShowText("Keine Antwort\ndurch Eigentuemer\nNochmal klingeln\noder warten.", 1);
}

void showRemoteDecision(const String &decision) {
  displayShowRemoteDecision(decision);
  blinkVerificationOutcome(decision == "approve");
}

void waitForMotionIdleBeforeSleep() {
  unsigned long startMillis = millis();
  while ((digitalRead(kMotionSensorPin) == HIGH || digitalRead(kButtonPin) == HIGH) &&
         millis() - startMillis < kMotionIdleBeforeSleepTimeoutMs) {
    logPrint(".");
    flushRemoteLogs();
    delay(kMotionIdlePollMs);
  }
  logPrintln();
}

void enterDeepSleepUntilMotion() {
  logPrintf("[SLEEP] Warte auf LOW an GPIO%d/Bewegung und GPIO%d/Taster vor Deep Sleep",
            kMotionSensorPin,
            kButtonPin);
  waitForMotionIdleBeforeSleep();

  if (digitalRead(kMotionSensorPin) == HIGH || digitalRead(kButtonPin) == HIGH) {
    logPrintln("[SLEEP] Wakeup-Pin bleibt HIGH, Deep Sleep wird uebersprungen.");
    return;
  }

  displayShowText("Schlafmodus\nBewegung/Taster\nweckt");
  logPrintf("[SLEEP] Kurz vor Deep Sleep. Wakeup bei HIGH an GPIO%d/Bewegung oder GPIO%d/Taster.\n",
            kMotionSensorPin,
            kButtonPin);
  flushRemoteLogs();
  delay(1200);
  displayTurnOff();

  setAccessLed(false);
  setFlashLed(false);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_ext1_wakeup(kDeepSleepWakeupPinMask, ESP_EXT1_WAKEUP_ANY_HIGH);
  esp_deep_sleep_start();
}

void finishRingSession(const String &responseBody) {
  if (!responseHasVerificationResult(responseBody)) {
    displayShowVerificationResult(responseBody);
    blinkAccessError();
    enterDeepSleepUntilMotion();
    return;
  }

  int eventId = jsonGetInt(responseBody, "event_id", -1);
  displayShowVerificationResult(responseBody);
  waitForTelegramDecision(eventId);
  enterDeepSleepUntilMotion();
}

int jsonGetInt(const String &payload, const char *key, int fallback) {
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

String jsonGetString(const String &payload, const char *key, const String &fallback) {
  String pattern = String("\"") + key + "\":\"";
  int start = payload.indexOf(pattern);
  if (start < 0) {
    return fallback;
  }
  start += pattern.length();
  int end = payload.indexOf('"', start);
  if (end < 0) {
    return fallback;
  }
  return payload.substring(start, end);
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

// Ein einzelnes Burst-Bild aufnehmen und die JPEG-Bytes in einen eigenen
// Puffer kopieren. Notwendig, weil die Kamera mit fb_count = 1 arbeitet und
// den Framebuffer sofort wieder freigeben muss, bevor das naechste Bild
// aufgenommen werden kann. So lassen sich mehrere Bilder kurz hintereinander
// aufnehmen (schnelle, gleichmaessige Blitze) und erst danach versenden.
struct CapturedImage {
  uint8_t *data;
  size_t len;
};

bool captureFrameToBuffer(CapturedImage *out) {
  out->data = nullptr;
  out->len = 0;

  camera_fb_t *frame = captureFrameWithFlash();
  if (frame == nullptr) {
    return false;
  }

  // Bevorzugt ins PSRAM kopieren, sonst Fallback ins interne RAM.
  uint8_t *copy = static_cast<uint8_t *>(ps_malloc(frame->len));
  if (copy == nullptr) {
    copy = static_cast<uint8_t *>(malloc(frame->len));
  }
  if (copy == nullptr) {
    logPrintln("[RING] Kein Speicher fuer Bildpuffer, Bild verworfen.");
    esp_camera_fb_return(frame);
    return false;
  }

  memcpy(copy, frame->buf, frame->len);
  out->data = copy;
  out->len = frame->len;
  esp_camera_fb_return(frame);
  return true;
}

String postFrameToPi(
    const String &url,
    const uint8_t *body,
    size_t bodyLen,
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
  *statusCode = http.POST(const_cast<uint8_t *>(body), bodyLen);
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

String pollRingDecisionFromPi(int eventId) {
  if (eventId < 0) {
    return "unavailable";
  }
  if (!ensureWifiConnected()) {
    return "unavailable";
  }

  String url = String(kRingDecisionBaseUrl) + "?event_id=" + String(eventId);
  HTTPClient http;
  http.setTimeout(5000);
  if (!http.begin(url)) {
    logPrintln("[TELEGRAM] Decision-URL konnte nicht initialisiert werden.");
    return "unavailable";
  }

  int statusCode = http.GET();
  if (statusCode <= 0) {
    logPrintf("[TELEGRAM] HTTP-Fehler bei Decision-Poll: %s\n", http.errorToString(statusCode).c_str());
    http.end();
    return "unavailable";
  }

  String responseBody = http.getString();
  http.end();
  logPrintf("[TELEGRAM] Decision-Antwort: %s\n", responseBody.c_str());

  if (!jsonHasTrue(responseBody, "telegram_enabled")) {
    return "disabled";
  }
  return jsonGetString(responseBody, "decision", "pending");
}

void waitForTelegramDecision(int eventId) {
  if (eventId < 0) {
    return;
  }

  displayShowRemoteDecision("pending");
  unsigned long startMillis = millis();
  while (millis() - startMillis < kTelegramDecisionTimeoutMs) {
    flushRemoteLogs();
    String decision = pollRingDecisionFromPi(eventId);
    if (decision == "approve" || decision == "deny") {
      logPrintf("[TELEGRAM] Entscheidung fuer Event %d: %s\n", eventId, decision.c_str());
      showRemoteDecision(decision);
      return;
    }
    if (decision == "disabled") {
      logPrintln("[TELEGRAM] Telegram ist auf dem Pi nicht aktiviert.");
      displayShowText("Telegram\nnicht aktiv", 1);
      return;
    }
    delay(kTelegramDecisionPollIntervalMs);
  }

  logPrintf("[TELEGRAM] Keine Entscheidung fuer Event %d innerhalb des Zeitlimits.\n", eventId);
  displayShowNoTelegramDecision();
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

  // Kein separates Startsignal-Blinken mehr: Die Blitz-LED leuchtet
  // ausschliesslich waehrend einer tatsaechlichen Bildaufnahme
  // (siehe captureFrameWithFlash), also genau einmal pro Burst-Foto.
  logPrintln("[RING] Starte Bildaufnahme (Blitz nur pro Foto).");
  delay(kRingSettleDelayMs);

  int matchedImages = 0;
  bool allSuccessful = true;
  int eventId = -1;
  String lastResponse = "{\"ok\":false,\"error\":\"Keine Antwort vom Pi erhalten.\"}";
  int detectedFaceImages = 0;

  // Phase 1: Alle Bilder zuegig hintereinander aufnehmen und puffern. Dadurch
  // kommen die Blitze kurz und gleichmaessig, unabhaengig von der spaeteren
  // Netzwerk- und Erkennungszeit.
  CapturedImage images[kBurstImageCount];
  for (int i = 0; i < kBurstImageCount; ++i) {
    images[i].data = nullptr;
    images[i].len = 0;
  }
  int capturedCount = 0;
  for (int i = 0; i < kBurstImageCount; ++i) {
    logPrintf("[RING] Nehme Bild %d/%d auf ...\n", i + 1, kBurstImageCount);
    if (captureFrameToBuffer(&images[capturedCount])) {
      logPrintf("[RING] Bild %d gepuffert. Groesse: %u Bytes\n", i + 1, images[capturedCount].len);
      capturedCount++;
    } else {
      logPrintln("[RING] Kamerabild konnte nicht gelesen werden.");
      allSuccessful = false;
      lastResponse = "{\"ok\":false,\"error\":\"Kamerabild konnte nicht gelesen werden.\"}";
    }
    if (i < kBurstImageCount - 1) {
      delay(kBurstPauseMs);
    }
  }

  // Phase 2: Die gepufferten Bilder nacheinander an den Pi senden.
  for (int i = 0; i < capturedCount; ++i) {
    int sequence = i + 1;
    logPrintf("[RING] Sende Bild %d/%d ...\n", sequence, capturedCount);

    String url = String(kRingCaptureBaseUrl);
    url += url.indexOf('?') >= 0 ? "&" : "?";
    url += "sequence=" + String(sequence);
    url += "&total=" + String(kBurstImageCount);
    if (eventId >= 0) {
      url += "&event_id=" + String(eventId);
    }

    int statusCode = -1;
    bool captureMatched = false;
    bool overallMatched = false;
    lastResponse = postFrameToPi(url, images[i].data, images[i].len, &statusCode, &captureMatched, &overallMatched, &eventId);

    if (statusCode <= 0) {
      allSuccessful = false;
    }
    if (captureMatched) {
      matchedImages++;
    }
    if (jsonGetInt(lastResponse, "detected_faces", 0) > 0) {
      detectedFaceImages++;
    }
  }

  // Puffer freigeben.
  for (int i = 0; i < capturedCount; ++i) {
    free(images[i].data);
    images[i].data = nullptr;
    images[i].len = 0;
  }

  gRingInProgress = false;
  bool accessGranted = matchedImages >= kRequiredMatchesForAccess;

  if (!allSuccessful && matchedImages == 0) {
    return lastResponse;
  }

  String summary = String("{\"ok\":true,\"event_id\":") + eventId +
                   ",\"matched_images\":" + matchedImages +
                   ",\"required_matches\":" + kRequiredMatchesForAccess +
                   ",\"face_detected\":" + (detectedFaceImages > 0 ? "true" : "false") +
                   ",\"detected_face_images\":" + detectedFaceImages +
                   ",\"overall_matched\":" + (accessGranted ? "true" : "false") +
                   ",\"last_response\":" + lastResponse + "}";
  logPrintf("[RING] Klingelereignis abgeschlossen: %s\n", summary.c_str());
  return summary;
}

String runRingWorkflowWithAccessLed() {
  setAccessLedVerifying();
  return runRingWorkflow();
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
  // Statische IP setzen, damit beim Verbinden kein DHCP-Lease ausgehandelt
  // werden muss. Schlaegt das fehl, faellt der ESP automatisch auf DHCP zurueck.
  if (!WiFi.config(kStaticIp, kGateway, kSubnet, kPrimaryDns)) {
    logPrintln("[WIFI] Statische IP konnte nicht gesetzt werden, nutze DHCP.");
  }
  WiFi.begin(kWifiSsid, kWifiPassword);

  logPrintln();
  logPrint("Verbinde mit WLAN");

  const unsigned long startMillis = millis();
  bool wifiBlinkEnabled = false;
  while (WiFi.status() != WL_CONNECTED && millis() - startMillis < 20000) {
    wifiBlinkEnabled = !wifiBlinkEnabled;
    setAccessLedWifiConnecting(wifiBlinkEnabled);
    logPrint(".");
    delay(500);
  }
  setAccessLedWifiConnecting(false);
  logPrintln();

  if (WiFi.status() != WL_CONNECTED) {
    logPrintln("WLAN-Verbindung fehlgeschlagen.");
    blinkAccessError();
    return false;
  }

  logPrint("WLAN verbunden. ESP-IP: ");
  logPrintln(WiFi.localIP().toString());
  logPrint("Verifier-Ziel: ");
  logPrintln(kVerifierUrl);
  setAccessLed(true);
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
  markUserActivity();
  String body = String("{\"ok\":true,\"ip\":\"") + WiFi.localIP().toString() +
                "\",\"ring_in_progress\":" + (gRingInProgress ? "true" : "false") + "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void handleSnapshot() {
  markUserActivity();
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
  markUserActivity();
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
  markUserActivity();
  String responseBody = runRingWorkflowWithAccessLed();
  bool ok = jsonHasTrue(responseBody, "ok");
  server.send(ok ? 200 : 500, "application/json; charset=utf-8", responseBody);
  delay(250);
  finishRingSession(responseBody);
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
  if (gStableButtonState == HIGH) {
    markUserActivity();
    logPrintln("[BUTTON] Taster gedrueckt, starte Verifikation.");
    ensureI2cDisplayReady();
    displayShowWelcome();
    delay(800);
    displayShowText("Pruefe\nGesicht...", 1);
    String responseBody = runRingWorkflowWithAccessLed();
    finishRingSession(responseBody);
  }
}

void handleMotionSensor() {
  bool reading = digitalRead(kMotionSensorPin);
  if (reading == gLastMotionReading) {
    return;
  }

  gLastMotionReading = reading;
  logPrintf("[MOTION] HC-SR501 OUT an GPIO%d: %s\n",
            kMotionSensorPin,
            reading == HIGH ? "Bewegung erkannt" : "keine Bewegung");
}

void handleIdleDeepSleep() {
  if (gRingInProgress || gLastUserActivityMs == 0) {
    return;
  }

  unsigned long idleMs = millis() - gLastUserActivityMs;
  if (idleMs < kIdleDeepSleepTimeoutMs) {
    return;
  }

  logPrintf("[SLEEP] Keine Aktivitaet seit %lu ms, gehe in Deep Sleep.\n", idleMs);
  enterDeepSleepUntilMotion();
  markUserActivity();
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
  logWakeupReason();

  pinMode(kFlashLedPin, OUTPUT);
  pinMode(kButtonPin, INPUT);
  pinMode(kMotionSensorPin, INPUT);
  setFlashLed(false);
  initAccessLed();
  gLastMotionReading = digitalRead(kMotionSensorPin);

  // Ohne funktionierende Kamera lohnt es sich nicht, WLAN und Server zu starten.
  if (!initCamera()) {
    logPrintln("Bitte Pinout und Stromversorgung pruefen, dann Reset druecken.");
    return;
  }

  // Danach Netzwerk und Webserver einschalten.
  connectToWifi();
  startServer();
  initI2cDisplay();
  markUserActivity();
  logPrintf("[BUTTON] Bereit. Taster an GPIO%d startet die Verifikation.\n", kButtonPin);
}

void loop() {
  // Eingehende Browser-Anfragen laufend bearbeiten.
  server.handleClient();
  handleButton();
  handleMotionSensor();
  flushRemoteLogs();
  handleIdleDeepSleep();
}
