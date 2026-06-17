#pragma once
// Vorlage für lokale Zugangsdaten.
//
// Ablauf:
//   1. Diese Datei zu  include/secrets.h  kopieren.
//   2. In secrets.h die echten WLAN-Zugangsdaten eintragen.
//   3. secrets.h NICHT committen (steht bereits in .gitignore).
//
// Hinweis: Das Auslagern ist keine Verschlüsselung. Es verhindert nur, dass die
// Zugangsdaten im Quellcode bzw. im Git-Repository landen. Wer den Flash des ESP
// physisch ausliest, kann sie weiterhin sehen (dafür: ESP32 Flash Encryption).

#define WIFI_SSID "deine-ssid"
#define WIFI_PASSWORD "dein-passwort"
