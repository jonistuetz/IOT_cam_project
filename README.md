# hs_IOT

Erster Prototyp fuer Gesichtverifikation mit ESP32-CAM und Raspberry Pi 4.

## Aufbau

- `src/main.cpp`: ESP32-CAM streamt weiter lokal und kann ein JPEG per HTTP an den Raspberry Pi schicken.
- `raspberry_pi/face_verifier.py`: InsightFace-basierter Verifikationsdienst mit `buffalo_sc`, ONNX Runtime CPU und SQLite-Logging.
- `raspberry_pi/requirements.txt`: Python-Abhaengigkeiten fuer den Pi.

## Raspberry Pi 4

Python-Umgebung einrichten und Modell laden:

```bash
cd raspberry_pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python face_verifier.py init-db
python face_verifier.py serve --host 0.0.0.0 --port 8000
```

Beim ersten Start laedt InsightFace das Modell `buffalo_sc` herunter. Fuer den Prototyp wird nur `CPUExecutionProvider` verwendet.

### Referenzbilder speichern

Mehrere Referenz-Embeddings pro Person sind vorgesehen. Fuer einen ersten Test pro Person 3-5 Bilder mit leicht variierenden Winkeln und Licht aufnehmen:

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

### Verifikation testen

Lokal:

```bash
python face_verifier.py verify-image --person-id jonathan --image /pfad/test.jpg
```

Per HTTP:

```bash
curl -X POST "http://PI_IP:8000/api/verify?person_id=jonathan" \
  -H "Content-Type: image/jpeg" \
  --data-binary @/pfad/test.jpg
```

Die Antworten enthalten `matched`, `similarity`, `threshold` und Fehlerdetails. Jede Verifikation wird in SQLite in `verification_logs` gespeichert.

## ESP32-CAM

In [src/main.cpp](/Users/jonathanstuetz/Documents/PlatformIO/Projects/hs_IOT/src/main.cpp:1) muessen noch drei Konstanten auf dein Netz angepasst werden:

- `kWifiSsid`
- `kWifiPassword`
- `kVerifierUrl`

Danach verbindet sich der ESP32-CAM mit demselben WLAN wie der Raspberry Pi, zeigt weiter den Livestream an und sendet bei `POST /verify` ein einzelnes JPEG an den Pi. Die Root-Seite bietet dafuer einen Button.

## SQLite

Die Datenbank liegt standardmaessig unter `raspberry_pi/face_verification.db` und enthaelt:

- `reference_embeddings`: mehrere Referenz-Embeddings pro Person
- `verification_logs`: Ergebnis, Similarity, Schwelle, Anzahl Referenzen und eventuelle Fehler

## Hinweise

- Der Standardwert fuer die Cosine-Similarity-Schwelle ist `0.42`. Das ist ein brauchbarer Startwert fuer den Prototypen, sollte aber mit echten Daten nachkalibriert werden.
- Der aktuelle ESP-Flow ist bewusst einfach gehalten: ein JPEG pro Anfrage statt Videostreaming zum Pi.
