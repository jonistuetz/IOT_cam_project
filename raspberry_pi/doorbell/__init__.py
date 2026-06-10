"""Smart-Doorbell-Dienst für den Raspberry Pi.

Aufbau des Pakets (von außen nach innen):

* ``constants`` / ``log``  – gemeinsame Konstanten und Logging.
* ``utils.images`` / ``utils.timeutils`` – reine Hilfsfunktionen.
* ``database`` / ``config`` – Persistenzschicht (SQLite, JSON-Config).
* ``recognition`` – Gesichtserkennung (InsightFace).
* ``system`` – WLAN, sicheres Herunterfahren, Netzwerkstatus.
* ``telegram`` – Benachrichtigung und Freigabe per Telegram.
* ``enrollment`` / ``verification`` / ``people`` / ``events`` – Fachlogik,
  als Mixins zusammengeführt in ``service.FaceVerifierService``.
* ``web`` – Flask-App und HTTP-Routen.

Einstiegspunkt ist ``run_doorbell.py`` im Projektordner.
"""
