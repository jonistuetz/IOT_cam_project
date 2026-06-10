"""Zentrale Konstanten und Standardpfade des Doorbell-Dienstes.

Alle Pfade beziehen sich auf das Projektverzeichnis (der Ordner, in dem
``run_doorbell.py`` liegt) – also das Elternverzeichnis dieses Pakets. Damit
bleiben Datenbank, Konfiguration und Captures an derselben Stelle wie zuvor,
als noch ``face_verifier.py`` direkt im Projektordner lag.
"""

from pathlib import Path

# Projektwurzel = Elternverzeichnis des doorbell-Pakets (raspberry_pi/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB_PATH = PROJECT_ROOT / "face_verification.db"
DEFAULT_CAPTURES_DIR = PROJECT_ROOT / "captures"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_MODEL_NAME = "buffalo_sc"
DEFAULT_SIMILARITY_THRESHOLD = 0.60
DEFAULT_DET_SIZE = (640, 640)

UNKNOWN_PERSON_ID = "unknown"
DEFAULT_ESP_SNAPSHOT_URL = "http://10.42.0.172/snapshot"
DEFAULT_REQUIRED_MATCHES_FOR_ACCESS = 2

SAFE_POWEROFF_HELPER = "/usr/local/sbin/hs-iot-safe-poweroff"
WIFI_SETUP_HELPER = "/usr/local/sbin/hs-iot-wifi-setup"
