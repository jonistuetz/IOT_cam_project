"""Zentraler Dienst, der die Fachlogik zusammenführt.

``FaceVerifierService`` ist der Orchestrator: er hält Konfiguration,
Datenbankpfad und die Kollaborateure (Gesichtserkennung, Telegram) und
kombiniert die fachlichen Mixins zu einer einzigen Klasse. Die übrigen Module
bleiben dadurch schlank und einzeln testbar.

Die zahlreichen kurzen ``_``-Methoden weiter unten sind bewusst dünne
Delegatoren auf die ausgelagerten Module. Sie behalten die alten Namen, damit
die Mixin-Methoden unverändert über ``self._foo`` zugreifen können.
"""

from pathlib import Path
from typing import Optional

from . import config as config_module
from . import database
from . import system
from .config import AppConfig
from .constants import (
    DEFAULT_CAPTURES_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL_NAME,
    DEFAULT_SIMILARITY_THRESHOLD,
)
from .enrollment import EnrollmentMixin
from .events import EventsMixin
from .log import debug as _log_debug
from .people import PeopleMixin
from .recognition import FaceRecognizer
from .telegram import TelegramNotifier
from .utils import images
from .utils import timeutils
from .verification import VerificationMixin


class FaceVerifierService(EnrollmentMixin, VerificationMixin, PeopleMixin, EventsMixin):
  def __init__(
      self,
      db_path: Path,
      config_path: Path = DEFAULT_CONFIG_PATH,
      model_name: str = DEFAULT_MODEL_NAME,
      similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
  ) -> None:
    self.db_path = Path(db_path)
    self.config_path = Path(config_path)
    self.captures_dir = DEFAULT_CAPTURES_DIR
    self.model_name = model_name
    self.similarity_threshold = similarity_threshold
    self.config = self._load_config(similarity_threshold)
    self.telegram_bot_token = self.config.telegram_bot_token
    self.telegram_chat_id = self.config.telegram_chat_id
    self.esp_snapshot_url = self.config.esp_snapshot_url
    self.similarity_threshold = self.config.similarity_threshold
    self.recognizer = FaceRecognizer(model_name=self.model_name)
    self.telegram = TelegramNotifier(self)
    self.captures_dir.mkdir(parents=True, exist_ok=True)
    self._ensure_default_threshold_persisted()

  # ----------------------------------------------------------------- Config
  def _ensure_default_threshold_persisted(self) -> None:
    config_module.ensure_default_threshold(self.config_path, self.similarity_threshold)

  def _load_config(self, fallback_similarity_threshold: Optional[float] = None) -> AppConfig:
    return config_module.load_config(self.config_path, fallback_similarity_threshold)

  def reload_config(self) -> None:
    self.config = self._load_config(self.similarity_threshold)
    self.telegram_bot_token = self.config.telegram_bot_token
    self.telegram_chat_id = self.config.telegram_chat_id
    self.esp_snapshot_url = self.config.esp_snapshot_url
    self.similarity_threshold = self.config.similarity_threshold

  def update_config(
      self,
      telegram_bot_token: Optional[str] = None,
      telegram_chat_id: Optional[str] = None,
      esp_snapshot_url: Optional[str] = None,
      similarity_threshold: Optional[str] = None,
  ) -> dict:
    current = self._load_config(self.similarity_threshold)
    next_similarity_threshold = current.similarity_threshold
    if similarity_threshold is not None and str(similarity_threshold).strip():
      try:
        next_similarity_threshold = min(max(float(similarity_threshold), 0.0), 1.0)
      except ValueError:
        raise ValueError("similarity_threshold muss eine Zahl zwischen 0 und 1 sein.")

    next_config = {
        "telegram_bot_token": (
            telegram_bot_token.strip()
            if telegram_bot_token is not None and telegram_bot_token.strip()
            else current.telegram_bot_token
        ),
        "telegram_chat_id": (
            telegram_chat_id.strip()
            if telegram_chat_id is not None and telegram_chat_id.strip()
            else current.telegram_chat_id
        ),
        "esp_snapshot_url": (
            esp_snapshot_url.strip()
            if esp_snapshot_url is not None and esp_snapshot_url.strip()
            else current.esp_snapshot_url
        ),
        "similarity_threshold": next_similarity_threshold,
    }

    config_module.write_config(self.config_path, next_config)
    self.reload_config()
    return self.config_status()

  def config_status(self) -> dict:
    return {
        "config_path": str(self.config_path),
        "config_exists": self.config_path.exists(),
        "telegram_enabled": self.telegram_enabled,
        "telegram_bot_token_masked": self._mask_secret(self.telegram_bot_token),
        "telegram_chat_id": self.telegram_chat_id,
        "esp_snapshot_url": self.esp_snapshot_url,
        "similarity_threshold": self.similarity_threshold,
        "setup_complete": self.setup_complete,
    }

  @property
  def setup_complete(self) -> bool:
    return self.telegram_enabled and len(self.list_person_ids()) > 0

  # ----------------------------------------------------------------- Database
  def init_db(self) -> None:
    database.init_schema(self.db_path)

  def _get_app_state(self, key: str) -> Optional[str]:
    return database.get_app_state(self.db_path, key)

  def _set_app_state(self, key: str, value: str) -> None:
    database.set_app_state(self.db_path, key, value)

  # --------------------------------------------------- Erkennung (Delegatoren)
  def _get_face_app(self):
    return self.recognizer

  def _extract_primary_face(self, image):
    return self.recognizer.extract_primary_face(image)

  @staticmethod
  def _decode_image(image_bytes: bytes):
    return images.decode_image(image_bytes)

  @staticmethod
  def _face_area(face) -> float:
    return images.face_area(face)

  @staticmethod
  def _get_normalized_embedding(face):
    return images.normalized_embedding(face)

  @staticmethod
  def _serialize_embedding(embedding) -> bytes:
    return images.serialize_embedding(embedding)

  @staticmethod
  def _deserialize_embedding(blob: bytes):
    return images.deserialize_embedding(blob)

  # ------------------------------------------------------ Telegram (Delegatoren)
  @property
  def telegram_enabled(self) -> bool:
    return self.telegram.enabled

  def _send_telegram_notification_for_event(self, event_id: int) -> None:
    self.telegram.notify_event(event_id)

  def _sync_telegram_updates(self) -> None:
    self.telegram.sync_updates()

  def send_telegram_test_message(self) -> dict:
    return self.telegram.send_test_message()

  # -------------------------------------------------------- System (Delegatoren)
  def network_status(self) -> dict:
    return system.network_status()

  def request_safe_shutdown(self) -> dict:
    return system.request_safe_shutdown()

  def wifi_setup(self, command: str, **kwargs) -> dict:
    return system.wifi_setup(command, **kwargs)

  # ---------------------------------------------------------- Sonstige Helfer
  @staticmethod
  def _debug(message: str) -> None:
    _log_debug(message)

  @staticmethod
  def _mask_secret(value: str) -> str:
    return config_module.mask_secret(value)

  @staticmethod
  def _utc_now() -> str:
    return timeutils.utc_now()

  @staticmethod
  def _to_local_time_label(utc_iso: str) -> str:
    return timeutils.to_local_time_label(utc_iso)

  @staticmethod
  def _local_time_label() -> str:
    return timeutils.local_time_label()
