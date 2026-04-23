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

// HTTP-Server auf Port 80 für die Geräte-API.
WebServer server(80);

// Kameraeinstellungen: kleine Aufloesung fuer schnelle Einzelbilder.
const framesize_t kFrameSize = FRAMESIZE_QVGA;
const int kJpegQuality = 12;
const int kFrameBufferCount = 2;
const int kFlashLedPin = 4;
const int kBurstImageCount = 3;
const unsigned long kRingSettleDelayMs = 700;
const unsigned long kFlashWarmupDelayMs = 120;
const unsigned long kBurstPauseMs = 180;

bool gRingInProgress = false;

void setFlashLed(bool enabled) {
  digitalWrite(kFlashLedPin, enabled ? HIGH : LOW);
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

  Serial.printf("[RING] Sende Bild an %s\n", url.c_str());
  if (!http.begin(url)) {
    Serial.println("[RING] Verifier-URL konnte nicht initialisiert werden.");
    return "{\"ok\":false,\"error\":\"Verifier-URL konnte nicht initialisiert werden.\"}";
  }

  http.addHeader("Content-Type", "image/jpeg");
  *statusCode = http.POST(frame->buf, frame->len);
  Serial.printf("[RING] POST abgeschlossen. Status: %d\n", *statusCode);

  if (*statusCode > 0) {
    responseBody = http.getString();
    Serial.printf("[RING] Antwort vom Pi: %s\n", responseBody.c_str());
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
    Serial.printf("[RING] HTTP-Fehler: %s\n", http.errorToString(*statusCode).c_str());
  }

  http.end();
  return responseBody;
}

String runRingWorkflow() {
  if (gRingInProgress) {
    return "{\"ok\":false,\"error\":\"Klingelworkflow laeuft bereits.\"}";
  }

  gRingInProgress = true;
  Serial.println("[RING] Klingelereignis gestartet.");

  if (WiFi.status() != WL_CONNECTED) {
    gRingInProgress = false;
    Serial.println("[RING] WLAN nicht verbunden.");
    return "{\"ok\":false,\"error\":\"ESP ist nicht mit dem WLAN verbunden.\"}";
  }

  Serial.println("[RING] Startsignal blinkt.");
  blinkStartSequence();
  delay(kRingSettleDelayMs);

  bool anyMatched = false;
  bool allSuccessful = true;
  int eventId = -1;
  String lastResponse = "{\"ok\":false,\"error\":\"Keine Antwort vom Pi erhalten.\"}";

  for (int sequence = 1; sequence <= kBurstImageCount; ++sequence) {
    Serial.printf("[RING] Erfasse Bild %d/%d ...\n", sequence, kBurstImageCount);
    camera_fb_t *frame = captureFrameWithFlash();
    if (frame == nullptr) {
      Serial.println("[RING] Kamerabild konnte nicht gelesen werden.");
      allSuccessful = false;
      lastResponse = "{\"ok\":false,\"error\":\"Kamerabild konnte nicht gelesen werden.\"}";
      continue;
    }

    Serial.printf("[RING] Bild %d vorhanden. Groesse: %u Bytes\n", sequence, frame->len);

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

    anyMatched = anyMatched || captureMatched || overallMatched;
    delay(kBurstPauseMs);
  }

  gRingInProgress = false;

  if (!allSuccessful && !anyMatched) {
    return lastResponse;
  }

  String summary = String("{\"ok\":true,\"event_id\":") + eventId +
                   ",\"overall_matched\":" + (anyMatched ? "true" : "false") +
                   ",\"last_response\":" + lastResponse + "}";
  Serial.printf("[RING] Klingelereignis abgeschlossen: %s\n", summary.c_str());
  return summary;
}

bool initCamera() {
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
  config.frame_size = kFrameSize;
  config.jpeg_quality = kJpegQuality;
  config.fb_count = kFrameBufferCount;
  config.grab_mode = CAMERA_GRAB_LATEST;

  // Kamera-Hardware initialisieren.
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Kamera-Start fehlgeschlagen. Fehlercode: 0x%x\n", err);
    return false;
  }

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

  Serial.println();
  Serial.print("Verbinde mit WLAN");

  const unsigned long startMillis = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startMillis < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WLAN-Verbindung fehlgeschlagen.");
    return false;
  }

  Serial.print("WLAN verbunden. ESP-IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("Verifier-Ziel: ");
  Serial.println(kVerifierUrl);
  return true;
}

void handleStatus() {
  String body = String("{\"ok\":true,\"ip\":\"") + WiFi.localIP().toString() +
                "\",\"ring_in_progress\":" + (gRingInProgress ? "true" : "false") + "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void handleSnapshot() {
  camera_fb_t *frame = captureFrameWithFlash();
  if (frame == nullptr) {
    Serial.println("[SNAPSHOT] Kamerabild konnte nicht gelesen werden.");
    server.send(500, "text/plain; charset=utf-8", "Kamerabild konnte nicht gelesen werden.");
    return;
  }

  server.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  server.send_P(200, "image/jpeg", reinterpret_cast<const char *>(frame->buf), frame->len);
  esp_camera_fb_return(frame);
}

void handleVerify() {
  Serial.println("[VERIFY] Anfrage vom Browser empfangen.");

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[VERIFY] WLAN nicht verbunden.");
    server.send(503, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"ESP ist nicht mit dem WLAN verbunden.\"}");
    return;
  }

  Serial.println("[VERIFY] Hole Kamerabild...");
  camera_fb_t *frame = captureFrameWithFlash();
  if (frame == nullptr) {
    Serial.println("[VERIFY] Kamerabild konnte nicht gelesen werden.");
    server.send(500, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"Kamerabild konnte nicht gelesen werden.\"}");
    return;
  }

  Serial.printf("[VERIFY] Kamerabild vorhanden. Groesse: %u Bytes\n", frame->len);

  HTTPClient http;
  http.setTimeout(15000);

  int statusCode = -1;
  String responseBody = "{\"ok\":false,\"error\":\"Unbekannter Fehler.\"}";

  Serial.printf("[VERIFY] Sende POST an %s\n", kVerifierUrl);
  if (http.begin(kVerifierUrl)) {
    http.addHeader("Content-Type", "image/jpeg");
    statusCode = http.POST(frame->buf, frame->len);
    Serial.printf("[VERIFY] POST abgeschlossen. Status: %d\n", statusCode);

    if (statusCode > 0) {
      responseBody = http.getString();
      Serial.printf("[VERIFY] Antwort vom Pi: %s\n", responseBody.c_str());
    } else {
      Serial.printf("[VERIFY] HTTP-Fehler: %s\n", http.errorToString(statusCode).c_str());
      responseBody = String("{\"ok\":false,\"error\":\"HTTP POST fehlgeschlagen: ") + http.errorToString(statusCode) + "\"}";
    }

    http.end();
  } else {
    Serial.println("[VERIFY] Verifier-URL konnte nicht initialisiert werden.");
    responseBody = "{\"ok\":false,\"error\":\"Verifier-URL konnte nicht initialisiert werden.\"}";
  }

  esp_camera_fb_return(frame);
  Serial.println("[VERIFY] Kamerabild freigegeben, sende Antwort an Browser.");

  server.send(statusCode > 0 ? statusCode : 502, "application/json; charset=utf-8", responseBody);
  Serial.println("[VERIFY] Browser-Antwort gesendet.");
}

void handleRingRequest() {
  String responseBody = runRingWorkflow();
  bool ok = jsonHasTrue(responseBody, "ok");
  server.send(ok ? 200 : 500, "application/json; charset=utf-8", responseBody);
}

void startServer() {
  // Geraete-API: Snapshot, Status und Ring-Trigger.
  server.on("/", HTTP_GET, handleStatus);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/snapshot", HTTP_GET, handleSnapshot);
  server.on("/verify", HTTP_POST, handleVerify);
  server.on("/ring", HTTP_POST, handleRingRequest);
  server.begin();
  Serial.println("Webserver gestartet.");
}

}  // namespace

void setup() {
  // Serielle Ausgabe zum Debuggen starten.
  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println("ESP32-CAM Live-Test startet...");

  pinMode(kFlashLedPin, OUTPUT);
  setFlashLed(false);

  // Ohne funktionierende Kamera lohnt es sich nicht, WLAN und Server zu starten.
  if (!initCamera()) {
    Serial.println("Bitte Pinout und Stromversorgung pruefen, dann Reset druecken.");
    return;
  }

  // Danach Netzwerk und Webserver einschalten.
  connectToWifi();
  startServer();
  Serial.println("[RING] Fuehre automatischen Klingeltest nach Boot aus.");
  runRingWorkflow();
}

void loop() {
  // Eingehende Browser-Anfragen laufend bearbeiten.
  server.handleClient();
}
