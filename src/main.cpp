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

// HTTP-Server auf Port 80 für die Weboberfläche.
WebServer server(80);

// Kameraeinstellungen: kleine Aufloesung fuer schnelle Einzelbilder.
const framesize_t kFrameSize = FRAMESIZE_QVGA;
const int kJpegQuality = 12;
const int kFrameBufferCount = 2;

// Einfache HTML-Seite fuer Einzelbildvorschau und manuelle Verifikation.
static const char kIndexHtml[] PROGMEM = R"HTML(
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ESP32-CAM Live Test v2</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4efe7;
      --card: #fffaf2;
      --text: #1f2933;
      --accent: #d97706;
    }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: linear-gradient(160deg, #f7f1e8 0%, #efe4d2 100%);
      color: var(--text);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }
    .card {
      width: min(100%, 900px);
      background: var(--card);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 20px 50px rgba(59, 41, 14, 0.15);
    }
    h1 {
      margin-top: 0;
    }
    img {
      width: 100%;
      border-radius: 16px;
      background: #000;
    }
    button {
      margin-top: 16px;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font-size: 16px;
      font-weight: 700;
      color: white;
      background: var(--accent);
      cursor: pointer;
    }
    button:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .actions {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }
    .meta {
      margin-top: 12px;
      line-height: 1.5;
    }
    pre {
      margin-top: 16px;
      background: #1f2933;
      color: #f8fafc;
      padding: 16px;
      border-radius: 14px;
      overflow: auto;
      min-height: 80px;
      white-space: pre-wrap;
    }
    code {
      background: rgba(217, 119, 6, 0.12);
      padding: 2px 6px;
      border-radius: 6px;
    }
    a {
      color: var(--accent);
    }
    .result-pending {
      background: #1f2933;
      color: #f8fafc;
    }
    .result-success {
      background: #14532d;
      color: #ecfdf5;
    }
    .result-fail {
      background: #7f1d1d;
      color: #fef2f2;
    }
    .result-title {
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .result-line {
      margin: 4px 0;
    }
    .result-raw {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid rgba(255, 255, 255, 0.2);
      font-size: 13px;
      opacity: 0.9;
    }
  </style>
</head>
<body>
  <main class="card">
    <h1>ESP32-CAM Verifikation</h1>
    <div class="meta"><code>UI-Version: v2</code></div>
    <img id="snapshotImage" src="/snapshot?t=0" alt="Aktuelles Kamerabild">
    <div class="meta">
      <div>Aktuelles Kamerabild</div>
      <div>Ein Klick auf den Button sendet genau ein JPEG an den Raspberry Pi.</div>
    </div>
    <div class="actions">
      <button id="refreshButton" type="button">Bild aktualisieren</button>
      <button id="verifyButton" type="button">Verifikation starten</button>
    </div>
    <pre id="result" class="result-pending">Noch keine Verifikation ausgefuehrt.</pre>
  </main>
  <script>
    const refreshButton = document.getElementById("refreshButton");
    const button = document.getElementById("verifyButton");
    const result = document.getElementById("result");
    const snapshotImage = document.getElementById("snapshotImage");

    function escapeHtml(value) {
      return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;");
    }

    function setResultState(stateClass, html) {
      result.className = stateClass;
      result.innerHTML = html;
    }

    function formatSimilarity(value) {
      return value == null ? "n/a" : Number(value).toFixed(3);
    }

    function renderResult(title, stateClass, details, rawText) {
      const detailHtml = details
          .map((detail) => `<div class="result-line"><strong>${escapeHtml(detail.label)}:</strong> ${escapeHtml(detail.value)}</div>`)
          .join("");
      const rawHtml = rawText
          ? `<div class="result-raw"><strong>Rohantwort:</strong><br>${escapeHtml(rawText)}</div>`
          : "";
      setResultState(
          stateClass,
          `<div class="result-title">${escapeHtml(title)}</div>${detailHtml}${rawHtml}`,
      );
    }

    function refreshSnapshot() {
      snapshotImage.src = "/snapshot?t=" + Date.now();
    }

    refreshButton.addEventListener("click", () => {
      refreshSnapshot();
    });

    button.addEventListener("click", async () => {
      button.disabled = true;
      refreshButton.disabled = true;
      renderResult("Sende Bild an den Raspberry Pi...", "result-pending", [], "");

      try {
        const response = await fetch("/verify", { method: "POST" });
        const text = await response.text();

        try {
          const payload = JSON.parse(text);
          const similarityText = formatSimilarity(payload.similarity);
          if (payload.ok && payload.matched) {
            renderResult(
                "Zugang erlaubt",
                "result-success",
                [
                  { label: "Confidence", value: similarityText },
                  { label: "Schwellwert", value: payload.threshold },
                  { label: "Erkannte Gesichter", value: payload.detected_faces },
                  { label: "Referenzen", value: payload.reference_count },
                  { label: "HTTP-Status", value: response.status },
                ],
                text,
            );
          } else if (payload.ok) {
            let reason = "Score unter Schwellwert.";
            if ((payload.detected_faces ?? 0) === 0) {
              reason = "Kein Gesicht erkannt.";
            } else if (payload.error) {
              reason = payload.error;
            }
            renderResult(
                "Kein Match",
                "result-fail",
                [
                  { label: "Confidence", value: similarityText },
                  { label: "Schwellwert", value: payload.threshold },
                  { label: "Grund", value: reason },
                  { label: "Erkannte Gesichter", value: payload.detected_faces },
                  { label: "Referenzen", value: payload.reference_count },
                  { label: "HTTP-Status", value: response.status },
                ],
                text,
            );
          } else {
            renderResult(
                "Fehler",
                "result-fail",
                [
                  { label: "Confidence", value: similarityText },
                  { label: "Grund", value: payload.error || "Unbekannter Fehler" },
                  { label: "HTTP-Status", value: response.status },
                ],
                text,
            );
          }
        } catch (error) {
          renderResult(
              response.ok ? "Antwort erhalten" : "Fehler",
              response.ok ? "result-success" : "result-fail",
              [{ label: "HTTP-Status", value: response.status }],
              text,
          );
        }
      } catch (error) {
        renderResult(
            "Fehler beim Senden",
            "result-fail",
            [{ label: "Grund", value: error }],
            "",
        );
      } finally {
        button.disabled = false;
        refreshButton.disabled = false;
      }
    });
  </script>
</body>
</html>
)HTML";

void sendIndexPage() {
  server.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  server.sendHeader("Pragma", "no-cache");
  server.send_P(200, "text/html; charset=utf-8", kIndexHtml);
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

void handleSnapshot() {
  camera_fb_t *frame = esp_camera_fb_get();
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
  camera_fb_t *frame = esp_camera_fb_get();
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

void startServer() {
  // Startseite, Einzelbild und Verifikationsroute mit ihren Funktionen verknuepfen.
  server.on("/", HTTP_GET, sendIndexPage);
  server.on("/snapshot", HTTP_GET, handleSnapshot);
  server.on("/verify", HTTP_POST, handleVerify);
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

  // Ohne funktionierende Kamera lohnt es sich nicht, WLAN und Server zu starten.
  if (!initCamera()) {
    Serial.println("Bitte Pinout und Stromversorgung pruefen, dann Reset druecken.");
    return;
  }

  // Danach Netzwerk und Webserver einschalten.
  connectToWifi();
  startServer();
}

void loop() {
  // Eingehende Browser-Anfragen laufend bearbeiten.
  server.handleClient();
}
