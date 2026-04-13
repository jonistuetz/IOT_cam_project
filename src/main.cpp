#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <esp_camera.h>

namespace {

// Zugangsdaten fuer das WLAN, das der ESP32-CAM selbst bereitstellt.
const char kAccessPointSsid[] = "ESP32-CAM-Test";
const char kAccessPointPassword[] = "12345678";

// HTTP-Server auf Port 80 für die Weboberfläche.
WebServer server(80);

// Kameraeinstellungen: kleine Aufloesung fuer stabilen Livestream.
const framesize_t kFrameSize = FRAMESIZE_QVGA;
const int kJpegQuality = 12;
const int kFrameBufferCount = 2;

void sendIndexPage() {
  // Einfache HTML-Seite, die nur den Livestream im Browser anzeigt.
  const char html[] = R"HTML(
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ESP32-CAM Live Test</title>
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
    .meta {
      margin-top: 12px;
      line-height: 1.5;
    }
    code {
      background: rgba(217, 119, 6, 0.12);
      padding: 2px 6px;
      border-radius: 6px;
    }
    a {
      color: var(--accent);
    }
  </style>
</head>
<body>
  <main class="card">
    <h1>ESP32-CAM Live Test</h1>
    <img src="/stream" alt="Live Kamerabild">
    <div class="meta">
      <div>Livebild der Kamera</div>
      <div>Wenn kein Bild erscheint, kurz den Reset-Knopf am Board druecken.</div>
    </div>
  </main>
</body>
</html>
)HTML";

  server.send(200, "text/html; charset=utf-8", html);
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

void handleStream() {
  // Die Browser-Anfrage fuer den Stream liefert fortlaufend neue JPEG-Bilder.
  WiFiClient client = server.client();

  client.println("HTTP/1.1 200 OK");
  client.println("Content-Type: multipart/x-mixed-replace; boundary=frame");
  client.println("Cache-Control: no-cache");
  client.println("Connection: close");
  client.println();

  while (client.connected()) {
    // Ein aktuelles Kamerabild aus dem Framebuffer holen.
    camera_fb_t *frame = esp_camera_fb_get();
    if (frame == nullptr) {
      Serial.println("Stream-Frame konnte nicht gelesen werden.");
      break;
    }

    // Ein einzelnes JPEG-Bild als Teil des MJPEG-Streams an den Browser senden.
    client.printf("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n", frame->len);
    client.write(frame->buf, frame->len);
    client.print("\r\n");

    // Das Bild nach dem Senden wieder freigeben.
    esp_camera_fb_return(frame);

    if (!client.connected()) {
      break;
    }

    // Kurze Pause zwischen zwei Bildern, damit der Stream stabil bleibt.
    delay(30);
  }
}

void startAccessPoint() {
  // Der ESP arbeitet als eigener WLAN-Hotspot, kein externer Router noetig.
  WiFi.mode(WIFI_AP);
  WiFi.softAP(kAccessPointSsid, kAccessPointPassword);

  IPAddress ip = WiFi.softAPIP();
  Serial.println();
  Serial.println("WLAN-Hotspot gestartet");
  Serial.print("SSID: ");
  Serial.println(kAccessPointSsid);
  Serial.print("Passwort: ");
  Serial.println(kAccessPointPassword);
  Serial.print("Weboberflaeche: http://");
  Serial.println(ip);
}

void startServer() {
  // Startseite und Livestream-Route mit ihren Funktionen verknuepfen.
  server.on("/", HTTP_GET, sendIndexPage);
  server.on("/stream", HTTP_GET, handleStream);
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
  startAccessPoint();
  startServer();
}

void loop() {
  // Eingehende Browser-Anfragen laufend bearbeiten.
  server.handleClient();
}
