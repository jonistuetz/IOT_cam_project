# hs_IOT

Prototyp einer smarten, vernetzten Türklingel mit `ESP32-CAM` als Kamera-/Geräteknoten und `Raspberry Pi` als Verifikations- und Setup-Server.

## Aktueller Stand

Der Prototyp bildet aktuell diesen Ablauf ab:

1. Der `ESP32-CAM` bootet und verbindet sich mit dem WLAN.
2. Ein externer Taster startet ein Klingel-/Verifikationsereignis.
3. Der ESP wartet kurz, blinkt als Startsignal und nimmt dann `3` Bilder mit Blitz auf.
4. Jedes Bild wird an den Raspberry Pi an `/api/ring-capture` geschickt.
5. Der Raspberry Pi führt für jedes Bild die Gesichtverifikation aus, speichert Snapshots und loggt das Gesamtereignis.
6. Optional sendet der Raspberry Pi ein Telegram-Foto mit Status und Buttons zum Freigeben oder Ablehnen.
7. Die externe LED blinkt nach der Verifikation langsam bei Zulassung und schnell bei Ablehnung.
8. Bei einer Telegram-Entscheidung zeigt das OLED die Rückmeldung an und der ESP blinkt das Ergebnis erneut.
9. Nach einer abgeschlossenen Klingelsession geht der ESP in Deep Sleep und wacht bei Bewegung am HC-SR501 wieder auf.
10. Die Pi-Setup-Seite bleibt als lokale Admin-Oberfläche für Telegram, Zulassungsschwelle, Internet-WLAN, Shutdown und Referenzbilder erreichbar.

## Rollen der Geräte

### Raspberry Pi

- hostet die Hauptoberfläche
- führt die InsightFace-Verifikation aus
- speichert Logs und Burst-Snapshots
- bietet Setup-Seite und APIs an

### ESP32-CAM

- verbindet sich mit dem WLAN
- nimmt Snapshots auf
- führt den Ring-/Burst-Workflow aus
- liefert nur noch eine kleine Geräte-API

Die HTML-Hauptoberfläche liegt **nicht mehr auf dem ESP**, sondern auf dem Pi.

## Projektstruktur

- [src/main.cpp](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/src/main.cpp): ESP32-CAM-Firmware
- [raspberry_pi/face_verifier.py](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/face_verifier.py): Flask-App, APIs, Verifikation, Datenbank, Telegram, WLAN-/Shutdown-Anbindung
- [raspberry_pi/templates/setup.html](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/templates/setup.html): HTML/CSS/JavaScript der Setup-/Admin-Seite
- [raspberry_pi/requirements.txt](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/requirements.txt): Python-Abhängigkeiten
- [raspberry_pi/install_autostart.sh](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/install_autostart.sh): installiert systemd-Service, Shutdown-Helper, WLAN-Helper und sudoers-Regeln
- [raspberry_pi/hs-iot-doorbell.service](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/hs-iot-doorbell.service): systemd-Service-Vorlage
- [raspberry_pi/config.example.json](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/config.example.json): Beispielkonfiguration ohne echte Secrets

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

Beim ersten Start lädt InsightFace automatisch das Modell `buffalo_sc` herunter. Der Prototyp nutzt nur `CPUExecutionProvider`.

### Setup-/Admin-Seite

Die veränderliche Nutzerkonfiguration wird lokal auf dem Pi verwaltet:

```text
http://PI_IP:8000/setup
```

Die Setup-Seite bleibt auch nach der Ersteinrichtung als Admin-Seite erreichbar. Die Startseite zeigt Kacheln für Telegram, Zulassungsschwelle, Shutdown, Internet-WLAN, Gesicht anlernen und gespeicherte Personen. Dort können aktuell:

- Telegram Bot Token und Chat ID gespeichert oder geändert werden
- die Zulassungsschwelle für Face-Matches angepasst werden
- der Internetzugang über den USB-WLAN-Adapter `wlan1` geprüft und verbunden werden
- eine Telegram-Testnachricht gesendet werden
- der Raspberry Pi sauber heruntergefahren werden
- vorhandene Personen angezeigt werden
- neue Personen oder weitere Referenzbilder angelernt werden
- Personen temporär deaktiviert oder wieder aktiviert werden
- Personen-Referenzbilder gelöscht werden, ohne alte Klingelereignisse zu löschen

Die echten Telegram-Daten werden in `raspberry_pi/config.json` gespeichert. Diese Datei ist in `.gitignore` eingetragen und sollte nicht ins Repository committed werden. Als Vorlage gibt es `raspberry_pi/config.example.json`.

Environment-Variablen bleiben als Fallback möglich:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCDEF..."
export TELEGRAM_CHAT_ID="123456789"
```

Der Bot sendet pro Klingelereignis ein Foto mit Caption und zwei Buttons:

- `Reinlassen`
- `Ablehnen`

Der ESP32-CAM behandelt die Gesichtserkennung nur als Empfehlung, fragt die finale Entscheidung bis zu 90 Sekunden beim Pi ab und zeigt danach `Telegram freigegeben`, `Telegram abgelehnt` oder bei Timeout eine kurze Hinweismeldung auf dem OLED an.

### Referenzbilder anlernen

Über die Setup-Seite können mehrere Bilder einer Person auf einmal hochgeladen werden. Intern wird pro gültigem Bild ein Face-Embedding berechnet und in der bestehenden SQLite-Datenbank gespeichert. Vorhandene Personen und Referenz-Embeddings bleiben dabei erhalten.

Alternativ kann die Setup-Seite Referenzbilder direkt mit der ESP32-CAM aufnehmen und anlernen. Dabei führt die Seite manuell durch 12 feste Aufnahmen: bei ca. 0,5 m und ca. 1 m Abstand jeweils 2 frontal, 2 leicht links und 2 leicht rechts. Jede Aufnahme wird einzeln per Button gestartet, damit die Person sich zwischen den Bildern in Ruhe positionieren kann. Das ist oft sinnvoll, weil Referenzbilder und spätere Klingelbilder dann von derselben Kamera und aus derselben Netzwerkstrecke stammen. Der ESP muss dafür wach und über seine Snapshot-URL erreichbar sein.

Eine laufende ESP-Anlernsession kann über die Setup-Seite verworfen werden. Dabei werden nur die Referenz-Embeddings dieser Session entfernt; ältere Referenzen derselben Person bleiben erhalten. Wenn eine Aufnahme innerhalb der geführten ESP-Session fehlschlägt, wird die aktuelle Session automatisch verworfen, damit keine unvollständigen Referenzen bestehen bleiben.

Deaktivierte Personen bleiben in der Datenbank gespeichert, werden aber für neue Verifikationen nicht mehr als Kandidaten genutzt. Beim Löschen einer Person werden nur die Referenz-Embeddings entfernt; historische `ring_events`, `ring_captures` und gespeicherte Capture-Bilder bleiben als Verlauf erhalten.

Alternativ per CLI:

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

In [src/main.cpp](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/src/main.cpp:1) müssen diese Konstanten zu deinem Netz passen:

- `kWifiSsid`
- `kWifiPassword`
- `kVerifierUrl`
- `kRingCaptureBaseUrl`
- `kRingDecisionBaseUrl`

Aktuell nimmt der ESP an, dass der Pi unter `10.42.0.1:8000` läuft.

### Aktuelle ESP-Logik

- `GPIO4` wird als Blitz-LED genutzt
- `GPIO12` wird als externer Taster genutzt, aktiv `HIGH` geschaltet und mit externem Pulldown nach `GND` stabilisiert
- `GPIO13` wird als I2C-SDA für Display und Bewegungssensor genutzt
- `GPIO14` wird als I2C-SCL für Display und Bewegungssensor genutzt
- `GPIO15` liest das OUT-Signal des HC-SR501-Bewegungssensors
- `GPIO2` schaltet die grüne Status-LED für WLAN, Prüfung und Ergebnis
- während der WLAN-Verbindung blinkt die grüne LED gedimmt als Start-/Verbindungsfeedback; nach erfolgreicher WLAN-Verbindung leuchtet sie dauerhaft
- nach Tastendruck wird auf dem Joy-IT SBC-OLED01 / SSD1306-OLED mit Adresse `0x3C` über `Adafruit_SSD1306` testweise `Herzlich Willkommen` angezeigt
- nach Boot wartet der ESP auf einen Tastendruck oder einen manuellen API-Aufruf
- während der Verifikation leuchtet Grün gedimmt; nach der Verifikation blinkt Grün langsam bei Zulassung und schnell bei Ablehnung
- nach einer abgeschlossenen Klingelsession wird Deep Sleep aktiviert, sobald HC-SR501-OUT und Taster wieder `LOW` sind
- wenn der ESP aufwacht, aber 90 Sekunden lang kein Klingelereignis und keine Geräte-API-Aktivität erfolgt, geht er ebenfalls wieder in Deep Sleep
- das OLED zeigt während der Prüfung und nach der Verifikation Statusmeldungen zu Gesichtserkennung und Zutrittsentscheidung
- nach einem Telegram-Ereignis wartet der ESP kurz auf eine Fernentscheidung und zeigt `Telegram freigegeben` oder `Telegram abgelehnt` an
- direkt vor Deep Sleep wird das OLED per SSD1306-Display-Off-Befehl ausgeschaltet
- vor dem Burst gibt es `2` langsame Startblinksignale
- vor jedem gespeicherten Foto verwirft der ESP alte Kameraframes, damit kein Bild aus dem vorherigen Ereignis im neuen Burst landet
- danach werden `3` Bilder mit jeweils kurzem Blitz aufgenommen
- die Bilder gehen an den Pi; Zutritt gilt erst ab mindestens `2` Matches im 3er-Burst

## Setup-Seite auf dem Pi

Die Hauptoberfläche ist:

```text
http://PI_IP:8000/setup
```

`GET /` leitet ebenfalls auf `/setup` weiter. Die Startseite zeigt ein kompaktes Raster aus Setup-Fenstern für Telegram, Zulassungsschwelle, Shutdown, Internet-WLAN, Gesicht anlernen und gespeicherte Personen.

## Relevante Endpunkte

### Pi

- `GET /` -> Weiterleitung auf `/setup`
- `GET /setup` -> Setup-/Admin-Seite
- `GET /health` -> Status des Pi-Dienstes
- `GET /api/network-status` -> Internet-/Telegram-Uplink-Check mit Debug-Infos
- `GET /api/config` -> gespeicherte Konfiguration ohne Klartext-Token anzeigen
- `POST /api/config` -> Telegram-Daten, ESP-Snapshot-URL oder Zulassungsschwelle speichern
- `POST /api/test-telegram` -> Telegram-Testnachricht senden
- `POST /api/system/shutdown` -> Raspberry Pi über den installierten Helper sauber herunterfahren
- `GET /api/persons` -> gespeicherte Personen und Referenzanzahl anzeigen
- `POST /api/persons/<id>/active` -> Person aktivieren oder deaktivieren
- `DELETE /api/persons/<id>` -> Referenzbilder einer Person löschen
- `GET /api/live-snapshot` -> Pi holt ein Snapshot vom ESP und reicht es weiter
- `POST /api/enroll` -> Referenzbild speichern
- `POST /api/enroll-batch` -> mehrere Referenzbilder in einem Request speichern
- `POST /api/enroll-from-esp` -> ein Referenzbild direkt von der ESP32-CAM holen und speichern
- `POST /api/enroll-session/discard` -> Referenzen einer geführten ESP-Anlernsession verwerfen
- `POST /api/verify` -> einzelnes Bild verifizieren
- `POST /api/ring-capture` -> einzelnes Burst-Bild innerhalb eines Klingelereignisses
- `POST /api/esp-log` -> ESP-Logzeilen empfangen und im Pi-Terminal ausgeben
- `GET /api/wifi/status` -> WLAN-Geräte und gespeicherte Profile anzeigen
- `POST /api/wifi/scan` -> WLANs über `wlan1` suchen
- `POST /api/wifi/connect` -> neues WLAN-Profil für `wlan1` verbinden
- `POST /api/wifi/activate` -> gespeichertes WLAN-Profil auf `wlan1` aktivieren
- `POST /api/wifi/priority` -> Priorität eines WLAN-Profils setzen
- `GET /api/ring-decision?event_id=<id>` -> aktuelle Telegram-Entscheidung für ein Klingelereignis
- `GET /captures/<datei>` -> gespeicherte Burst-Bilder

### ESP

- `GET /` -> Status-JSON
- `GET /status` -> Status-JSON
- `GET /snapshot` -> aktuelles JPEG
- `POST /verify` -> Debug-Verify eines Einzelbilds
- `POST /ring` -> Ring-Workflow manuell auslösen

## Logging und Datenhaltung

Die Datenbank liegt standardmäßig unter:

```text
raspberry_pi/face_verification.db
```

Aktuell verwendete Tabellen:

- `reference_embeddings`
- `verification_logs`
- `ring_events`
- `ring_captures`
- `event_actions`
- `app_state`
- `person_settings`

Zusätzlich werden Burst-Bilder gespeichert unter:

```text
raspberry_pi/captures/
```

## Typischer Entwicklungsablauf

### Pi-Code deployen

Bei Änderungen an Python- oder Template-Dateien reicht normalerweise ein Datei-Deploy und ein Service-Neustart:

```bash
cd /Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT
rsync -avz raspberry_pi/ pi4-direct:/home/pi4/iot_project/
```

Dann auf dem Pi:

```bash
ssh pi4-direct
cd /home/pi4/iot_project
sudo systemctl restart hs-iot-doorbell
sudo journalctl -u hs-iot-doorbell -f
```

Wenn der Dienst nicht über systemd läuft, kann er stattdessen manuell aus der virtuellen Umgebung gestartet werden:

```bash
cd /home/pi4/iot_project
source .venv/bin/activate
python face_verifier.py serve --host 0.0.0.0 --port 8000
```

Wenn [raspberry_pi/requirements.txt](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/requirements.txt) geändert wurde, zuerst im Projektordner auf dem Pi die Abhängigkeiten aktualisieren:

```bash
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart hs-iot-doorbell
```

### Autostart per systemd

Der Autostart wird auf dem Pi aus dem aktuellen Projektordner installiert. Dadurch ist es egal, ob der Ordner z. B. `/home/pi4/iot_project` oder `/home/pi4/IoT_Project` heißt:

```bash
cd /home/pi4/IoT_Project
./install_autostart.sh
```

Das Skript erkennt automatisch `.venv`, `.iotp` oder `python3`, schreibt `/etc/systemd/system/hs-iot-doorbell.service`, aktiviert den Service und startet ihn direkt.

Danach kann der Dienst so geprüft werden:

```bash
sudo systemctl status hs-iot-doorbell
sudo journalctl -u hs-iot-doorbell -f
```

Der Service startet nur den Normalbetrieb:

```text
python face_verifier.py serve --host 0.0.0.0 --port 8000
```

SSH bleibt weiterhin normal möglich. Wenn der Service bereits läuft, darf `python face_verifier.py serve` nicht zusätzlich manuell auf Port `8000` gestartet werden. Für Wartung:

```bash
sudo systemctl stop hs-iot-doorbell
sudo systemctl start hs-iot-doorbell
sudo systemctl restart hs-iot-doorbell
```

Wenn beim manuellen Start diese Meldung erscheint:

```text
Address already in use
Port 8000 is in use by another program.
```

dann läuft der Autostart-Service bereits. In diesem Fall entweder nur die Logs beobachten:

```bash
sudo systemctl status hs-iot-doorbell
sudo journalctl -u hs-iot-doorbell -f
```

oder den Service vor einem manuellen Start stoppen:

```bash
sudo systemctl stop hs-iot-doorbell
source .iotp/bin/activate  # oder .venv, je nach Installation
python face_verifier.py serve --host 0.0.0.0 --port 8000
```

Nach dem manuellen Test den Normalbetrieb wieder aktivieren:

```bash
sudo systemctl start hs-iot-doorbell
```

Requirements, WLAN-/Hotspot-Einrichtung und Datenbank-Backups gehören zur einmaligen Vorbereitung und werden nicht bei jedem Boot automatisch verändert.

Das Autostart-Skript installiert zusätzlich einen eng begrenzten Shutdown-Helper:

```text
/usr/local/sbin/hs-iot-safe-poweroff
```

Nur dieser feste Helper darf vom Pi-Benutzer ohne Passwort per `sudo` ausgeführt werden. Dadurch funktioniert der Button `Raspberry Pi sicher herunterfahren` auf `/setup`, ohne der Web-App allgemeine Root-Rechte zu geben.

Außerdem installiert das Autostart-Skript einen begrenzten WLAN-Helper:

```text
/usr/local/sbin/hs-iot-wifi-setup
```

Die Setup-Seite nutzt diesen Helper für das Fenster `Internet-WLAN`. Verwaltet wird ausschließlich der Internetadapter `wlan1`; der lokale Hotspot auf `wlan0` wird angezeigt, aber nicht verändert. Dadurch können gespeicherte Profile wie Zuhause oder Hochschule angezeigt, gesucht und bei Bedarf auf `wlan1` aktiviert werden, ohne den lokalen Setup-Hotspot zu überschreiben.

Das Install-Skript muss nicht nach jeder Änderung an [raspberry_pi/face_verifier.py](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/face_verifier.py) oder [raspberry_pi/templates/setup.html](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/raspberry_pi/templates/setup.html) erneut ausgeführt werden. Es ist nur nötig, wenn sich Service-Datei, Autostart-Skript, Shutdown-Helper, WLAN-Helper, sudoers-Regeln oder die grundsätzliche Projektinstallation geändert haben:

```bash
cd /home/pi4/iot_project
./install_autostart.sh
```

Kurzregel:

- Python/HTML geändert -> Dateien deployen, `sudo systemctl restart hs-iot-doorbell`
- `requirements.txt` geändert -> `pip install -r requirements.txt`, dann Service neu starten
- `hs-iot-doorbell.service` oder `install_autostart.sh` geändert -> `./install_autostart.sh`
- ESP-Firmware geändert -> ESP32-CAM neu flashen

### ESP-Code deployen

- Firmware in PlatformIO bauen
- auf den ESP32-CAM flashen
- seriellen Monitor beobachten

PlatformIO installiert die ESP-Abhängigkeiten aus `lib_deps` automatisch beim Build. Für das OLED-Display werden aktuell `Adafruit SSD1306` und `Adafruit GFX Library` verwendet.

## Hinweise

- Der Standard-Schwellwert für Cosine Similarity ist `0.60`.
- Die Schwellwert-Konfiguration kann über `/setup` in `raspberry_pi/config.json` gespeichert werden.
- Die ESP-Vorschau auf der Setup-Seite nutzt aktuell eine fest hinterlegte ESP-IP (`DEFAULT_ESP_SNAPSHOT_URL`). Wenn sich die ESP-IP ändert, muss diese Konstante angepasst werden.
- `GPIO15`, `GPIO12` und `GPIO2` sind Boot-Strapping-Pins. Externe Beschaltungen dürfen diese Pins beim Einschalten nicht hart auf `GND` oder `3.3V` ziehen.
- `GPIO15` und `GPIO12` sind RTC-GPIOs und wecken den ESP32 per Deep-Sleep-Wakeup, wenn der HC-SR501 `OUT` oder der Taster `HIGH` wird.
- Für den Taster-Wakeup muss der Taster `GPIO12` auf `3.3V` ziehen; ein externer Pulldown, z. B. `100k` nach `GND`, hält den Pin im Ruhezustand `LOW`.
- `GPIO12` ist ebenfalls ein Boot-Strapping-Pin. Der Taster darf beim Einschalten nicht gedrückt sein.
- `GPIO16` wird beim ESP32-CAM mit PSRAM nicht als Taster-Pin genutzt, weil er die Kamera-/PSRAM-Initialisierung stören kann.
