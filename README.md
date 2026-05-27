# hs_IOT

Prototyp einer smarten, vernetzten Tuerklingel mit `ESP32-CAM` als Kamera-/Geraeteknoten und `Raspberry Pi` als Verifikations- und Dashboard-Server.

## Aktueller Stand

Der Prototyp bildet aktuell diesen Ablauf ab:

1. Der `ESP32-CAM` bootet und verbindet sich mit dem WLAN.
2. Ein externer Taster startet ein Klingel-/Verifikationsereignis.
3. Der ESP wartet kurz, blinkt als Startsignal und nimmt dann `3` Bilder mit Blitz auf.
4. Jedes Bild wird an den Raspberry Pi an `/api/ring-capture` geschickt.
5. Der Raspberry Pi fuehrt fuer jedes Bild die Gesichtverifikation aus, speichert Snapshots und loggt das Gesamtereignis.
6. Optional sendet der Raspberry Pi ein Telegram-Foto mit Status und Buttons zum Freigeben oder Ablehnen.
7. Die externe LED blinkt nach der Verifikation langsam bei Zulassung und schnell bei Ablehnung.
8. Bei einer Telegram-Entscheidung zeigt das OLED die Rueckmeldung an und der ESP blinkt das Ergebnis erneut.
9. Nach einer abgeschlossenen Klingelsession geht der ESP in Deep Sleep und wacht bei Bewegung am HC-SR501 wieder auf.
10. Das Pi-Dashboard zeigt:
   - Livebild vom ESP (nur auf Knopfdruck über Dashboard)
   - letztes Klingelereignis
   - Burst-Snapshots
   - Verlauf mit Filtern

## Rollen der Geraete

### Raspberry Pi

- hostet die Hauptoberflaeche
- fuehrt die InsightFace-Verifikation aus
- speichert Logs und Burst-Snapshots
- bietet Dashboard und APIs an

### ESP32-CAM

- verbindet sich mit dem WLAN
- nimmt Snapshots auf
- fuehrt den Ring-/Burst-Workflow aus
- liefert nur noch eine kleine Geraete-API

Die HTML-Hauptoberflaeche liegt **nicht mehr auf dem ESP**, sondern auf dem Pi.

## Projektstruktur

- [src/main.cpp](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/src/main.cpp): ESP32-CAM-Firmware
- [raspberry_pi/face_verifier.py](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/face_verifier.py): Flask-App, Verifikation, Dashboard, Logging
- [raspberry_pi/requirements.txt](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/requirements.txt): Python-Abhaengigkeiten

## Raspberry Pi einrichten

### Python-Umgebung

```bash
cd /home/pi4/iot_project
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python face_verifier.py init-db
```

Danach den Dienst starten:

```bash
cd /home/pi4/iot_project
source .venv/bin/activate
python face_verifier.py serve --host 0.0.0.0 --port 8000
```

Beim ersten Start laedt InsightFace automatisch das Modell `buffalo_sc` herunter. Der Prototyp nutzt nur `CPUExecutionProvider`.

### Telegram-Benachrichtigungen aktivieren

Der Pi kann nach einem abgeschlossenen Klingelereignis ein Foto an einen Telegram-Chat senden. Dafuer vor dem Start des Dienstes diese Umgebungsvariablen setzen:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCDEF..."
export TELEGRAM_CHAT_ID="123456789"
```

Danach den Pi-Dienst wie oben starten. Der Bot sendet pro Klingelereignis ein Foto mit Caption und zwei Buttons:

- `Reinlassen`
- `Ablehnen`

Der ESP32-CAM behandelt die Gesichtserkennung nur als Empfehlung, fragt die finale Entscheidung bis zu 90 Sekunden beim Pi ab und zeigt danach `Telegram freigegeben`, `Telegram abgelehnt` oder bei Timeout eine kurze Hinweismeldung auf dem OLED an.

### Referenzbilder anlernen

```bash
python face_verifier.py enroll-image --person-id jonathan --image /pfad/bild1.jpg --note frontal
python face_verifier.py enroll-image --person-id jonathan --image /pfad/bild2.jpg --note links
python face_verifier.py enroll-image --person-id jonathan --image /pfad/bild3.jpg --note rechts
```

Alternativ per HTTP:

```bash
curl -X POST http://PI_IP:8000/api/enroll \
  -F person_id=jonathan \
  -F image=@/pfad/bild1.jpg
```

## ESP32-CAM konfigurieren

In [src/main.cpp](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/src/main.cpp:1) muessen diese Konstanten zu deinem Netz passen:

- `kWifiSsid`
- `kWifiPassword`
- `kVerifierUrl`
- `kRingCaptureBaseUrl`
- `kRingDecisionBaseUrl`

Aktuell nimmt der ESP an, dass der Pi unter `10.42.0.1:8000` laeuft.

### Aktuelle ESP-Logik

- `GPIO4` wird als Blitz-LED genutzt
- `GPIO12` wird als externer Taster genutzt, aktiv `HIGH` geschaltet und mit externem Pulldown nach `GND` stabilisiert
- `GPIO13` wird als I2C-SDA fuer Display und Bewegungssensor genutzt
- `GPIO14` wird als I2C-SCL fuer Display und Bewegungssensor genutzt
- `GPIO15` liest das OUT-Signal des HC-SR501-Bewegungssensors
- `GPIO2` schaltet die gruene Status-LED fuer WLAN, Pruefung und Ergebnis
- waehrend der WLAN-Verbindung blinkt die gruene LED gedimmt als Start-/Verbindungsfeedback; nach erfolgreicher WLAN-Verbindung leuchtet sie dauerhaft
- nach Tastendruck wird auf dem Joy-IT SBC-OLED01 / SSD1306-OLED mit Adresse `0x3C` ueber `Adafruit_SSD1306` testweise `Herzlich Willkommen` angezeigt
- nach Boot wartet der ESP auf einen Tastendruck oder einen manuellen API-Aufruf
- waehrend der Verifikation leuchtet Gruen gedimmt; nach der Verifikation blinkt Gruen langsam bei Zulassung und schnell bei Ablehnung
- nach einer abgeschlossenen Klingelsession wird Deep Sleep aktiviert, sobald HC-SR501-OUT und Taster wieder `LOW` sind
- das OLED zeigt waehrend der Pruefung und nach der Verifikation Statusmeldungen zu Gesichtserkennung und Zutrittsentscheidung
- nach einem Telegram-Ereignis wartet der ESP kurz auf eine Fernentscheidung und zeigt `Telegram freigegeben` oder `Telegram abgelehnt` an
- direkt vor Deep Sleep wird das OLED per SSD1306-Display-Off-Befehl ausgeschaltet
- vor dem Burst gibt es `2` langsame Startblinksignale
- vor jedem gespeicherten Foto verwirft der ESP alte Kameraframes, damit kein Bild aus dem vorherigen Ereignis im neuen Burst landet
- danach werden `3` Bilder mit jeweils kurzem Blitz aufgenommen
- die Bilder gehen an den Pi; Zutritt gilt erst ab mindestens `2` Matches im 3er-Burst

## Dashboard auf dem Pi

Die Hauptoberflaeche ist:

```text
http://PI_IP:8000/
```

Aktuell bietet das Dashboard:

- Livebild vom ESP ueber Pi-Proxy
- letztes Klingelereignis
- gespeicherte Burst-Snapshots
- Verlauf
- Filter im Verlaufsfenster:
  - Person
  - Tag
  - Status (`Match` / `kein Match`)

Das Livebild aktualisiert sich **nur auf Knopfdruck**.

## Relevante Endpunkte

### Pi

- `GET /` -> Dashboard
- `GET /health` -> Status des Pi-Dienstes
- `GET /api/network-status` -> Internet-/Telegram-Uplink-Check mit Debug-Infos
- `GET /api/dashboard` -> Dashboard-Daten als JSON
- `GET /api/live-snapshot` -> Pi holt ein Snapshot vom ESP und reicht es weiter
- `POST /api/enroll` -> Referenzbild speichern
- `POST /api/verify` -> einzelnes Bild verifizieren
- `POST /api/ring-capture` -> einzelnes Burst-Bild innerhalb eines Klingelereignisses
- `POST /api/esp-log` -> ESP-Logzeilen empfangen und im Pi-Terminal ausgeben
- `GET /api/ring-decision?event_id=<id>` -> aktuelle Telegram-Entscheidung fuer ein Klingelereignis
- `GET /captures/<datei>` -> gespeicherte Burst-Bilder

### ESP

- `GET /` -> Status-JSON
- `GET /status` -> Status-JSON
- `GET /snapshot` -> aktuelles JPEG
- `POST /verify` -> Debug-Verify eines Einzelbilds
- `POST /ring` -> Ring-Workflow manuell ausloesen

## Logging und Datenhaltung

Die Datenbank liegt standardmaessig unter:

```text
raspberry_pi/face_verification.db
```

Aktuell verwendete Tabellen:

- `reference_embeddings`
- `verification_logs`
- `ring_events`
- `ring_captures`

Zusatzlich werden Burst-Bilder gespeichert unter:

```text
raspberry_pi/captures/
```

## Typischer Entwicklungsablauf

### Pi-Code deployen

```bash
cd /Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT
rsync -avz raspberry_pi/ pi4-direct:/home/pi4/iot_project/
```

Dann auf dem Pi:

```bash
ssh pi4-direct
cd /home/pi4/iot_project
source .venv/bin/activate
python face_verifier.py serve
```

### ESP-Code deployen

- Firmware in PlatformIO bauen
- auf den ESP32-CAM flashen
- seriellen Monitor beobachten

PlatformIO installiert die ESP-Abhaengigkeiten aus `lib_deps` automatisch beim Build. Fuer das OLED-Display werden aktuell `Adafruit SSD1306` und `Adafruit GFX Library` verwendet.

## Hinweise

- Der Standard-Schwellwert fuer Cosine Similarity ist `0.42`.
- Das Livebild im Pi-Dashboard nutzt aktuell eine fest hinterlegte ESP-IP (`DEFAULT_ESP_SNAPSHOT_URL`). Wenn sich die ESP-IP aendert, muss diese Konstante angepasst werden.
- `GPIO15`, `GPIO12` und `GPIO2` sind Boot-Strapping-Pins. Externe Beschaltungen duerfen diese Pins beim Einschalten nicht hart auf `GND` oder `3.3V` ziehen.
- `GPIO15` und `GPIO12` sind RTC-GPIOs und wecken den ESP32 per Deep-Sleep-Wakeup, wenn der HC-SR501 `OUT` oder der Taster `HIGH` wird.
- Fuer den Taster-Wakeup muss der Taster `GPIO12` auf `3.3V` ziehen; ein externer Pulldown, z. B. `100k` nach `GND`, haelt den Pin im Ruhezustand `LOW`.
- `GPIO12` ist ebenfalls ein Boot-Strapping-Pin. Der Taster darf beim Einschalten nicht gedrueckt sein.
- `GPIO16` wird beim ESP32-CAM mit PSRAM nicht als Taster-Pin genutzt, weil er die Kamera-/PSRAM-Initialisierung stoeren kann.
