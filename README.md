# hs_IOT

Smarte Türklingel mit Gesichtserkennung. Eine **ESP32-CAM** nimmt beim Klingeln Bilder auf, ein **Raspberry Pi** prüft das Gesicht und schickt eine Telegram-Nachricht mit Freigabe-Buttons. Die Gesichtserkennung liefert dabei nur eine Empfehlung – die endgültige Freigabe erfolgt per Telegram.

## Funktionsweise

1. Taster am ESP startet ein Klingelereignis; der ESP nimmt 3 Bilder mit Blitz auf.
2. Die Bilder gehen an den Pi (`/api/ring-capture`), der jedes Gesicht prüft.
3. Die Gesichtserkennung erstellt daraus nur eine Empfehlung (Gesicht „erkannt" ab mindestens 2 Treffern im 3er-Burst).
4. Der Pi sendet ein Telegram-Foto mit den Buttons *Reinlassen* / *Ablehnen* – darüber fällt die eigentliche Zutrittsentscheidung.
5. Der ESP wartet auf die Telegram-Antwort und zeigt das Ergebnis über Status-LED und OLED an.
6. Danach geht der ESP in Deep Sleep und wacht bei Bewegung (HC-SR501) wieder auf.

Die Bedienoberfläche (Setup/Admin) läuft auf dem Pi, **nicht** auf dem ESP.

## Projektstruktur

- `src/main.cpp` – ESP32-CAM-Firmware
- `include/secrets.example.h` – Vorlage für die WLAN-Zugangsdaten (siehe ESP-Einrichtung)
- `raspberry_pi/run_doorbell.py` – Einstiegspunkt des Pi-Dienstes
- `raspberry_pi/doorbell/` – Python-Paket mit der gesamten Pi-Logik (Erkennung, Telegram, Web, Datenbank …)
- `raspberry_pi/deploy/` – `install_autostart.sh` und die systemd-Service-Vorlage
- `raspberry_pi/config.example.json` – Beispielkonfiguration ohne echte Secrets

## Raspberry Pi einrichten

Einmalig auf dem Pi:

```bash
cd /home/pi4/iot_project
python3 -m venv .iotp
source .iotp/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python run_doorbell.py init-db
```

Beim ersten Start lädt InsightFace automatisch das Modell `buffalo_sc` (nur CPU).

Danach den Autostart einrichten – das Skript erstellt den systemd-Service und startet ihn:

```bash
./deploy/install_autostart.sh
```

Es installiert zusätzlich zwei eng begrenzte Helfer, damit die Setup-Seite den Pi sauber herunterfahren und das Internet-WLAN (`wlan1`) verwalten kann – beides ohne der Web-App allgemeine Root-Rechte zu geben.

## ESP32-CAM einrichten

Die WLAN-Zugangsdaten stehen **nicht** im Code. Vor dem ersten Build die Vorlage kopieren und ausfüllen:

```bash
cp include/secrets.example.h include/secrets.h
# danach in include/secrets.h WIFI_SSID und WIFI_PASSWORD eintragen
```

`include/secrets.h` ist gitignored; fehlt die Datei, bricht der Build mit klarer Meldung ab. Anschließend in PlatformIO bauen und auf die ESP32-CAM flashen. Die Ziel-Adresse des Pi (`10.42.0.1:8000`) steht als Konstante in `src/main.cpp`.

## Setup-/Admin-Seite

Von einem Gerät im Pi-Hotspot (`hs-iot`) erreichbar unter:

```text
http://10.42.0.1:8000/setup
```

`10.42.0.1` ist die feste Hotspot-Adresse des Pi (`wlan0`); über das Uplink-WLAN (`wlan1`) gilt stattdessen die per DHCP vergebene IP.

Auf der Seite lassen sich u. a. Telegram (Token/Chat-ID), die Zulassungsschwelle und das Internet-WLAN konfigurieren, Personen anlernen/aktivieren/löschen sowie der Pi sicher herunterfahren. Die echten Telegram-Daten landen in `raspberry_pi/config.json` (gitignored).

## Wichtige Befehle (SSH/Wartung)

Code auf den Pi übertragen (vom Entwicklungsrechner):

```bash
rsync -avz raspberry_pi/ pi4-direct:/home/pi4/iot_project/
```

Auf dem Pi den Dienst verwalten und Logs ansehen:

```bash
ssh pi4-direct
sudo systemctl restart hs-iot-doorbell   # neu starten (z. B. nach Code-Update)
sudo systemctl status  hs-iot-doorbell   # läuft er?
sudo systemctl stop    hs-iot-doorbell   # stoppen (z. B. für manuellen Test)
sudo journalctl -u hs-iot-doorbell -f    # Live-Log
```

Manuell starten (nur wenn der Service vorher gestoppt ist – sonst ist Port 8000 belegt):

```bash
cd /home/pi4/iot_project
source .iotp/bin/activate
python run_doorbell.py serve --host 0.0.0.0 --port 8000
```

Kurzregel, was nach welcher Änderung nötig ist:

- Python/HTML geändert → Dateien per `rsync` deployen, dann `sudo systemctl restart hs-iot-doorbell`
- `requirements.txt` geändert → `pip install -r requirements.txt`, dann Service neu starten
- Service-/Deploy-Skript geändert → `./deploy/install_autostart.sh` erneut ausführen
- ESP-Firmware geändert → neu flashen

## Daten

- Datenbank: `raspberry_pi/face_verification.db` (Personen, Logs, Klingelereignisse)
- Gespeicherte Bilder: `raspberry_pi/captures/`

Beim Löschen einer Person werden nur ihre Referenzbilder entfernt; die Klingel-Historie bleibt erhalten.

## Hinweise

- Standard-Schwelle für die Gesichtsähnlichkeit (Cosine Similarity): `0.60`, anpassbar über `/setup`.
- Die ESP-Vorschau auf der Setup-Seite nutzt eine fest hinterlegte ESP-IP (`esp_snapshot_url` in der Config). Ändert sich die ESP-IP, muss dieser Wert angepasst werden.
- `GPIO12` (Taster) und `GPIO15` (Bewegungssensor) wecken den ESP aus dem Deep Sleep und sind ESP32-Boot-Strapping-Pins. Kritisch ist nur `GPIO12`: er muss beim Einschalten `LOW` sein, dafür sorgt der Pulldown (~100 kΩ) am Taster – der Taster darf beim Boot also nicht gedrückt sein (würde `GPIO12` auf `HIGH` ziehen und den Boot stören). Der HC-SR501 an `GPIO15` braucht keinen Widerstand, da sein Push-Pull-Ausgang den Pegel aktiv vorgibt.
