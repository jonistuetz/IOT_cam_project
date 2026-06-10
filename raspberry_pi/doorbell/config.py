"""Laden, Schreiben und Maskieren der lokalen JSON-Konfiguration.

Die Konfiguration umfasst Telegram-Zugang, ESP-Snapshot-URL und die
Ähnlichkeitsschwelle. Werte können zusätzlich über Umgebungsvariablen kommen;
ein in der Datei gesetzter Wert hat Vorrang.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_ESP_SNAPSHOT_URL, DEFAULT_SIMILARITY_THRESHOLD
from .log import debug


@dataclass
class AppConfig:
  telegram_bot_token: str = ""
  telegram_chat_id: str = ""
  esp_snapshot_url: str = DEFAULT_ESP_SNAPSHOT_URL
  similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD


def mask_secret(value: str) -> str:
  if not value:
    return ""
  if len(value) <= 8:
    return "*" * len(value)
  return f"{value[:4]}...{value[-4:]}"


def write_config(config_path: Path, data: dict) -> None:
  """Schreibe die Config als JSON und setze restriktive Dateirechte (0600)."""
  config_path.parent.mkdir(parents=True, exist_ok=True)
  config_path.write_text(
      json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
  )
  try:
    os.chmod(config_path, 0o600)
  except OSError as exc:
    debug(f"config chmod failed: {exc}")


def ensure_default_threshold(config_path: Path, threshold: float) -> None:
  """Stelle sicher, dass die Config eine Schwelle enthaelt.

  Standardwert ist 0.60. Wird nur geschrieben, wenn noch kein Wert in der
  Datei steht – ein bereits vom Nutzer gesetzter Wert bleibt unangetastet
  und wird nur auf ausdruecklichen Wunsch ueber die Oberflaeche geaendert.
  """
  existing: dict = {}
  if config_path.exists():
    try:
      loaded = json.loads(config_path.read_text(encoding="utf-8"))
      if isinstance(loaded, dict):
        existing = loaded
    except (OSError, json.JSONDecodeError) as exc:
      debug(f"config read for default persist failed: {exc}")
  if existing.get("similarity_threshold") is not None:
    return
  existing["similarity_threshold"] = threshold
  try:
    write_config(config_path, existing)
    debug(f"Standard-Schwelle {threshold:.2f} in Config hinterlegt.")
  except OSError as exc:
    debug(f"default threshold persist failed: {exc}")


def load_config(config_path: Path, fallback_similarity_threshold: Optional[float] = None) -> AppConfig:
  payload = {}
  if config_path.exists():
    try:
      payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      debug(f"config load failed, using fallbacks: {exc}")

  telegram_bot_token = str(
      payload.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
  ).strip()
  telegram_chat_id = str(
      payload.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
  ).strip()
  esp_snapshot_url = str(
      payload.get("esp_snapshot_url") or os.environ.get("ESP_SNAPSHOT_URL", DEFAULT_ESP_SNAPSHOT_URL)
  ).strip()
  threshold_value = payload.get("similarity_threshold")
  if threshold_value is None:
    threshold_value = os.environ.get("SIMILARITY_THRESHOLD")
  try:
    similarity_threshold = float(
        threshold_value
        if threshold_value is not None and str(threshold_value).strip()
        else (
            fallback_similarity_threshold
            if fallback_similarity_threshold is not None
            else DEFAULT_SIMILARITY_THRESHOLD
        )
    )
  except (TypeError, ValueError):
    similarity_threshold = (
        fallback_similarity_threshold
        if fallback_similarity_threshold is not None
        else DEFAULT_SIMILARITY_THRESHOLD
    )
  similarity_threshold = min(max(similarity_threshold, 0.0), 1.0)

  return AppConfig(
      telegram_bot_token=telegram_bot_token,
      telegram_chat_id=telegram_chat_id,
      esp_snapshot_url=esp_snapshot_url or DEFAULT_ESP_SNAPSHOT_URL,
      similarity_threshold=similarity_threshold,
  )
