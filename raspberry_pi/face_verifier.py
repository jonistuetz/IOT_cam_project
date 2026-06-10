import argparse
import json
import os
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, make_response, redirect, render_template, request, send_from_directory
from insightface.app import FaceAnalysis


DEFAULT_DB_PATH = Path(__file__).with_name("face_verification.db")
DEFAULT_CAPTURES_DIR = Path(__file__).with_name("captures")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_MODEL_NAME = "buffalo_sc"
DEFAULT_SIMILARITY_THRESHOLD = 0.60
DEFAULT_DET_SIZE = (640, 640)
UNKNOWN_PERSON_ID = "unknown"
DEFAULT_ESP_SNAPSHOT_URL = "http://10.42.0.172/snapshot"
DEFAULT_REQUIRED_MATCHES_FOR_ACCESS = 2
SAFE_POWEROFF_HELPER = "/usr/local/sbin/hs-iot-safe-poweroff"
WIFI_SETUP_HELPER = "/usr/local/sbin/hs-iot-wifi-setup"



@dataclass
class VerificationResult:
  person_id: str
  matched: bool
  similarity: Optional[float]
  threshold: float
  reference_count: int
  detected_faces: int
  error: Optional[str] = None


@dataclass
class AppConfig:
  telegram_bot_token: str = ""
  telegram_chat_id: str = ""
  esp_snapshot_url: str = DEFAULT_ESP_SNAPSHOT_URL
  similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD


class FaceVerifierService:
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
    self._face_app: Optional[FaceAnalysis] = None
    self.captures_dir.mkdir(parents=True, exist_ok=True)
    self._ensure_default_threshold_persisted()

  def _ensure_default_threshold_persisted(self) -> None:
    """Stelle sicher, dass die Config eine Schwelle enthaelt.

    Standardwert ist 0.60. Wird nur geschrieben, wenn noch kein Wert in der
    Datei steht – ein bereits vom Nutzer gesetzter Wert bleibt unangetastet
    und wird nur auf ausdruecklichen Wunsch ueber die Oberflaeche geaendert.
    """
    existing: dict = {}
    if self.config_path.exists():
      try:
        loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
          existing = loaded
      except (OSError, json.JSONDecodeError) as exc:
        self._debug(f"config read for default persist failed: {exc}")
    if existing.get("similarity_threshold") is not None:
      return
    existing["similarity_threshold"] = self.similarity_threshold
    try:
      self.config_path.parent.mkdir(parents=True, exist_ok=True)
      self.config_path.write_text(
          json.dumps(existing, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
      )
      try:
        os.chmod(self.config_path, 0o600)
      except OSError as exc:
        self._debug(f"config chmod failed: {exc}")
      self._debug(
          f"Standard-Schwelle {self.similarity_threshold:.2f} in Config hinterlegt."
      )
    except OSError as exc:
      self._debug(f"default threshold persist failed: {exc}")

  @staticmethod
  def _debug(message: str) -> None:
    print(f"[face-verifier] {message}", flush=True)

  def _load_config(self, fallback_similarity_threshold: Optional[float] = None) -> AppConfig:
    payload = {}
    if self.config_path.exists():
      try:
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
      except (OSError, json.JSONDecodeError) as exc:
        self._debug(f"config load failed, using fallbacks: {exc}")

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

    self.config_path.write_text(json.dumps(next_config, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    try:
      os.chmod(self.config_path, 0o600)
    except OSError as exc:
      self._debug(f"config chmod failed: {exc}")

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

  @staticmethod
  def _mask_secret(value: str) -> str:
    if not value:
      return ""
    if len(value) <= 8:
      return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"

  def init_db(self) -> None:
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS reference_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT NOT NULL,
            embedding BLOB NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS verification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT NOT NULL,
            matched INTEGER NOT NULL,
            similarity REAL,
            threshold REAL NOT NULL,
            reference_count INTEGER NOT NULL,
            detected_faces INTEGER NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS ring_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT NOT NULL,
            total_images INTEGER NOT NULL,
            received_images INTEGER NOT NULL DEFAULT 0,
            matched_images INTEGER NOT NULL DEFAULT 0,
            matched INTEGER NOT NULL DEFAULT 0,
            best_similarity REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS ring_captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            sequence_index INTEGER NOT NULL,
            matched INTEGER NOT NULL,
            similarity REAL,
            threshold REAL NOT NULL,
            detected_faces INTEGER NOT NULL,
            error TEXT,
            image_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES ring_events(id)
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS event_actions (
            event_id INTEGER PRIMARY KEY,
            telegram_message_id INTEGER,
            telegram_notified_at TEXT,
            decision TEXT,
            decision_source TEXT,
            decided_at TEXT,
            FOREIGN KEY(event_id) REFERENCES ring_events(id)
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS person_settings (
            person_id TEXT PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
          )
          """
      )
      self._ensure_column(
          connection,
          table_name="ring_events",
          column_name="matched_images",
          column_definition="INTEGER NOT NULL DEFAULT 0",
      )
      connection.commit()

  def enroll_embedding(self, person_id: str, image_bytes: bytes, note: Optional[str] = None) -> dict:
    image = self._decode_image(image_bytes)
    face = self._extract_primary_face(image)
    embedding = self._get_normalized_embedding(face)
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO reference_embeddings (person_id, embedding, note, created_at)
          VALUES (?, ?, ?, ?)
          """,
          (person_id, self._serialize_embedding(embedding), note, now),
      )
      connection.execute(
          """
          INSERT INTO person_settings (person_id, active, updated_at)
          VALUES (?, 1, ?)
          ON CONFLICT(person_id) DO UPDATE SET
            active = 1,
            updated_at = excluded.updated_at
          """,
          (person_id, now),
      )
      connection.commit()

      reference_count = connection.execute(
          "SELECT COUNT(*) FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      ).fetchone()[0]

    return {
        "person_id": person_id,
        "reference_count": reference_count,
        "embedding_dim": int(embedding.shape[0]),
    }

  def verify(
      self,
      person_id: Optional[str],
      image_bytes: bytes,
      threshold: Optional[float] = None,
  ) -> VerificationResult:
    similarity_threshold = self.similarity_threshold if threshold is None else threshold
    requested_person_id = person_id
    self._debug(
        f"verify start requested_person_id={requested_person_id or 'ALL'} threshold={similarity_threshold:.3f}"
    )

    try:
      image = self._decode_image(image_bytes)
      faces = self._get_face_app().get(image)
      if len(faces) == 0:
        self._debug("verify result no face detected")
        result = VerificationResult(
            person_id=requested_person_id or UNKNOWN_PERSON_ID,
            matched=False,
            similarity=None,
            threshold=similarity_threshold,
            reference_count=self._reference_count(requested_person_id) if requested_person_id else 0,
            detected_faces=0,
            error="Kein Gesicht erkannt.",
        )
        self._log_verification(result)
        return result

      if len(faces) > 1:
        faces.sort(key=self._face_area, reverse=True)

      probe_embedding = self._get_normalized_embedding(faces[0])
      if requested_person_id:
        if not self._person_is_active(requested_person_id):
          self._debug(f"verify aborted because person is inactive: {requested_person_id}")
          result = VerificationResult(
              person_id=requested_person_id,
              matched=False,
              similarity=None,
              threshold=similarity_threshold,
              reference_count=self._reference_count(requested_person_id),
              detected_faces=len(faces),
              error="Person ist deaktiviert.",
          )
          self._log_verification(result)
          return result
        candidate_people = [requested_person_id]
      else:
        candidate_people = self.list_person_ids()
      self._debug(f"verify candidates={candidate_people}")

      if not candidate_people:
        self._debug("verify aborted because no candidates are enrolled")
        result = VerificationResult(
            person_id=requested_person_id or UNKNOWN_PERSON_ID,
            matched=False,
            similarity=None,
            threshold=similarity_threshold,
            reference_count=0,
            detected_faces=len(faces),
            error="Keine Referenz-Embeddings gespeichert.",
        )
        self._log_verification(result)
        return result

      best_person_id = requested_person_id or UNKNOWN_PERSON_ID
      best_similarity: Optional[float] = None
      best_reference_count = 0
      candidate_summaries: list[str] = []

      for candidate_person_id in candidate_people:
        references = self._load_reference_embeddings(candidate_person_id)
        if references.size == 0:
          candidate_summaries.append(f"{candidate_person_id}:refs=0")
          continue

        similarities = np.dot(references, probe_embedding)
        candidate_best_similarity = float(np.max(similarities))
        candidate_summaries.append(
            f"{candidate_person_id}:refs={len(references)} best={candidate_best_similarity:.4f}"
        )
        if best_similarity is None or candidate_best_similarity > best_similarity:
          best_similarity = candidate_best_similarity
          best_person_id = candidate_person_id
          best_reference_count = len(references)

      self._debug("verify candidate_results=" + ", ".join(candidate_summaries))

      if best_similarity is None:
        self._debug("verify finished without any usable reference embeddings")
        result = VerificationResult(
            person_id=requested_person_id or UNKNOWN_PERSON_ID,
            matched=False,
            similarity=None,
            threshold=similarity_threshold,
            reference_count=0,
            detected_faces=len(faces),
            error=(
                "Keine Referenz-Embeddings für diese Person gespeichert."
                if requested_person_id
                else "Keine gültigen Referenz-Embeddings gespeichert."
            ),
        )
        self._log_verification(result)
        return result

      matched = best_similarity >= similarity_threshold
      self._debug(
          "verify winner="
          f"{best_person_id} similarity={best_similarity:.4f} "
          f"threshold={similarity_threshold:.3f} matched={matched}"
      )
      result = VerificationResult(
          person_id=best_person_id,
          matched=matched,
          similarity=best_similarity,
          threshold=similarity_threshold,
          reference_count=best_reference_count,
          detected_faces=len(faces),
      )
      self._log_verification(result)
      return result
    except Exception as exc:  # pragma: no cover - defensive logging for prototype runtime
      self._debug(f"verify exception: {exc}")
      result = VerificationResult(
          person_id=requested_person_id or UNKNOWN_PERSON_ID,
          matched=False,
          similarity=None,
          threshold=similarity_threshold,
          reference_count=self._reference_count(requested_person_id) if requested_person_id else 0,
          detected_faces=0,
          error=str(exc),
      )
      self._log_verification(result)
      return result

  def list_people(self) -> list[dict]:
    with sqlite3.connect(self.db_path) as connection:
      rows = connection.execute(
          """
          SELECT
            reference_embeddings.person_id,
            COUNT(*) AS reference_count,
            MAX(reference_embeddings.created_at) AS updated_at,
            COALESCE(person_settings.active, 1) AS active
          FROM reference_embeddings
          LEFT JOIN person_settings
            ON person_settings.person_id = reference_embeddings.person_id
          GROUP BY reference_embeddings.person_id, COALESCE(person_settings.active, 1)
          ORDER BY reference_embeddings.person_id
          """
      ).fetchall()

    return [
        {
            "person_id": row[0],
            "reference_count": row[1],
            "updated_at": row[2],
            "active": bool(row[3]),
        }
        for row in rows
    ]

  def list_person_ids(self, include_inactive: bool = False) -> list[str]:
    return [
        entry["person_id"]
        for entry in self.list_people()
        if include_inactive or entry["active"]
    ]

  def set_person_active(self, person_id: str, active: bool) -> dict:
    if self._reference_count(person_id) == 0:
      raise ValueError("Person hat keine gespeicherten Referenzbilder.")

    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO person_settings (person_id, active, updated_at)
          VALUES (?, ?, ?)
          ON CONFLICT(person_id) DO UPDATE SET
            active = excluded.active,
            updated_at = excluded.updated_at
          """,
          (person_id, int(active), self._utc_now()),
      )
      connection.commit()

    return {"person_id": person_id, "active": active}

  def delete_person(self, person_id: str) -> dict:
    with sqlite3.connect(self.db_path) as connection:
      cursor = connection.execute(
          "DELETE FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      )
      deleted_references = cursor.rowcount
      connection.execute(
          "DELETE FROM person_settings WHERE person_id = ?",
          (person_id,),
      )
      connection.commit()

    return {"person_id": person_id, "deleted_references": deleted_references}

  def discard_enroll_session(self, person_id: str, session_id: str) -> dict:
    if not person_id:
      raise ValueError("person_id fehlt.")
    if not session_id:
      raise ValueError("session_id fehlt.")

    marker = f"session:{session_id}"
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO app_state (key, value)
          VALUES (?, ?)
          ON CONFLICT(key) DO UPDATE SET value = excluded.value
          """,
          (self._discarded_session_key(session_id), self._utc_now()),
      )
      cursor = connection.execute(
          """
          DELETE FROM reference_embeddings
          WHERE person_id = ?
            AND note LIKE ?
          """,
          (person_id, f"%{marker}%"),
      )
      deleted_references = cursor.rowcount
      remaining_references = connection.execute(
          "SELECT COUNT(*) FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      ).fetchone()[0]
      if remaining_references == 0:
        connection.execute(
            "DELETE FROM person_settings WHERE person_id = ?",
            (person_id,),
        )
      connection.commit()

    return {
        "person_id": person_id,
        "session_id": session_id,
        "deleted_references": deleted_references,
        "remaining_references": remaining_references,
    }

  def _discarded_session_key(self, session_id: str) -> str:
    return f"discarded_enroll_session:{session_id}"

  def _is_enroll_session_discarded(self, session_id: Optional[str]) -> bool:
    if not session_id:
      return False
    return self._get_app_state(self._discarded_session_key(session_id)) is not None

  def _person_is_active(self, person_id: str) -> bool:
    with sqlite3.connect(self.db_path) as connection:
      row = connection.execute(
          "SELECT active FROM person_settings WHERE person_id = ?",
          (person_id,),
      ).fetchone()
    return row is None or bool(row[0])

  def handle_ring_capture(
      self,
      person_id: Optional[str],
      image_bytes: bytes,
      sequence_index: int,
      total_images: int,
      event_id: Optional[int] = None,
      threshold: Optional[float] = None,
  ) -> dict:
    if sequence_index < 1:
      raise ValueError("sequence_index muss >= 1 sein.")
    if total_images < 1:
      raise ValueError("total_images muss >= 1 sein.")

    self._debug(
        f"ring_capture start requested_person_id={person_id or 'ALL'} "
        f"sequence={sequence_index}/{total_images} event_id={event_id}"
    )

    result = self.verify(person_id=person_id, image_bytes=image_bytes, threshold=threshold)
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      if event_id is None:
        cursor = connection.execute(
            """
            INSERT INTO ring_events (person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at)
            VALUES (?, ?, 0, 0, 0, NULL, ?, ?)
            """,
            (result.person_id, total_images, now, now),
        )
        event_id = int(cursor.lastrowid)

      image_filename = f"ring_{event_id}_{sequence_index}_{int(datetime.now(timezone.utc).timestamp() * 1000)}.jpg"
      image_path = self.captures_dir / image_filename
      image_path.write_bytes(image_bytes)

      connection.execute(
          """
          INSERT INTO ring_captures (
            event_id,
            sequence_index,
            matched,
            similarity,
            threshold,
            detected_faces,
            error,
            image_path,
            created_at
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
              event_id,
              sequence_index,
              int(result.matched),
              result.similarity,
              result.threshold,
              result.detected_faces,
              result.error,
              image_filename,
              now,
          ),
      )

      event_row = connection.execute(
          "SELECT received_images, matched_images, matched, best_similarity, total_images FROM ring_events WHERE id = ?",
          (event_id,),
      ).fetchone()
      received_images = int(event_row[0]) + 1
      matched_images = int(event_row[1]) + (1 if result.matched else 0)
      required_matches = min(DEFAULT_REQUIRED_MATCHES_FOR_ACCESS, total_images)
      event_matched = matched_images >= required_matches
      current_best = event_row[3]
      best_similarity = current_best
      if result.similarity is not None and (best_similarity is None or result.similarity > best_similarity):
        best_similarity = result.similarity

      connection.execute(
          """
          UPDATE ring_events
          SET person_id = ?, received_images = ?, matched_images = ?, matched = ?, best_similarity = ?, updated_at = ?
          WHERE id = ?
          """,
          (result.person_id, received_images, matched_images, int(event_matched), best_similarity, now, event_id),
      )
      connection.commit()

    event_complete = received_images >= total_images
    if event_complete:
      self._send_telegram_notification_for_event(event_id)

    self._debug(
        f"ring_capture result event_id={event_id} winner={result.person_id} "
        f"matched={result.matched} similarity={result.similarity} "
        f"matched_images={matched_images}/{required_matches} overall_matched={event_matched}"
    )

    return {
        "ok": result.error is None,
        "event_id": event_id,
        "sequence_index": sequence_index,
        "total_images": total_images,
        "matched": result.matched,
        "similarity": result.similarity,
        "threshold": result.threshold,
        "reference_count": result.reference_count,
        "detected_faces": result.detected_faces,
        "error": result.error,
        "event_complete": event_complete,
        "matched_images": matched_images,
        "required_matches": required_matches,
        "overall_matched": event_matched,
        "overall_best_similarity": best_similarity,
        "image_url": f"/captures/{image_filename}",
    }

  def get_event_decision(self, event_id: int) -> dict:
    self._sync_telegram_updates()
    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      action = connection.execute(
          """
          SELECT event_id, decision, decision_source, decided_at
          FROM event_actions
          WHERE event_id = ?
          """,
          (event_id,),
      ).fetchone()

    decision = "pending"
    source = None
    decided_at = None
    if action is not None and action["decision"]:
      decision = action["decision"]
      source = action["decision_source"]
      decided_at = action["decided_at"]

    return {
        "ok": True,
        "event_id": event_id,
        "decision": decision,
        "decision_source": source,
        "decided_at": decided_at,
        "telegram_enabled": self.telegram_enabled,
    }

  def fetch_live_snapshot(self, attempts: int = 3, retry_delay_s: float = 1.0) -> bytes:
    separator = "&" if "?" in self.esp_snapshot_url else "?"
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
      snapshot_url = f"{self.esp_snapshot_url}{separator}t={int(datetime.now(timezone.utc).timestamp() * 1000)}"
      try:
        with urlrequest.urlopen(snapshot_url, timeout=8) as response:
          return response.read()
      except Exception as exc:
        last_error = exc
        self._debug(f"snapshot fetch failed attempt={attempt}/{attempts}: {exc}")
        if attempt < attempts:
          time.sleep(retry_delay_s)

    raise last_error if last_error is not None else RuntimeError("Snapshot konnte nicht geladen werden.")

  def enroll_from_esp(
      self,
      person_id: str,
      count: int = 1,
      note: Optional[str] = None,
      session_id: Optional[str] = None,
  ) -> dict:
    if not person_id:
      raise ValueError("person_id fehlt.")
    if count < 1 or count > 10:
      raise ValueError("count muss zwischen 1 und 10 liegen.")

    self._debug(f"enroll_from_esp start person_id={person_id} count={count} url={self.esp_snapshot_url}")
    results = []
    success_count = 0
    latest_image_url = None

    for index in range(1, count + 1):
      try:
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        image_bytes = self.fetch_live_snapshot()
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        image_filename = (
            f"reference_{person_id}_{index}_{int(datetime.now(timezone.utc).timestamp() * 1000)}.jpg"
        )
        image_path = self.captures_dir / image_filename
        image_path.write_bytes(image_bytes)

        self._debug(
            f"enroll_from_esp image {index}/{count} bytes={len(image_bytes)} filename={image_filename}"
        )
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        payload = self.enroll_embedding(
            person_id=person_id,
            image_bytes=image_bytes,
            note=note or f"esp-camera:{image_filename}",
        )
        success_count += 1
        latest_image_url = f"/captures/{image_filename}"
        results.append({"ok": True, "image_url": latest_image_url, **payload})
        self._debug(
            f"enroll_from_esp image {index}/{count} ok reference_count={payload['reference_count']}"
        )
      except Exception as exc:
        self._debug(f"enroll_from_esp image {index}/{count} failed: {exc}")
        results.append({"ok": False, "error": str(exc)})

    self._debug(f"enroll_from_esp done person_id={person_id} successful={success_count}/{count}")
    return {
        "ok": success_count > 0,
        "person_id": person_id,
        "requested_images": count,
        "successful_images": success_count,
        "failed_images": count - success_count,
        "latest_image_url": latest_image_url,
        "results": results,
    }

  def network_status(self) -> dict:
    target_host = "api.telegram.org"
    status = {
        "internet_ok": False,
        "dns_ok": False,
        "tcp_ok": False,
        "target_host": target_host,
        "resolved_ip": None,
        "local_ip": None,
        "error": None,
    }

    try:
      probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      probe_socket.settimeout(2.0)
      probe_socket.connect(("1.1.1.1", 80))
      status["local_ip"] = probe_socket.getsockname()[0]
      probe_socket.close()
    except OSError as exc:
      status["error"] = f"Kein Uplink für Internet-Test: {exc}"
      return status

    try:
      resolved_ip = socket.gethostbyname(target_host)
      status["resolved_ip"] = resolved_ip
      status["dns_ok"] = True
    except OSError as exc:
      status["error"] = f"DNS-Auflösung für {target_host} fehlgeschlagen: {exc}"
      return status

    try:
      tcp_socket = socket.create_connection((target_host, 443), timeout=4.0)
      tcp_socket.close()
      status["tcp_ok"] = True
      status["internet_ok"] = True
    except OSError as exc:
      status["error"] = f"HTTPS-Verbindung zu {target_host}:443 fehlgeschlagen: {exc}"

    return status

  def _event_to_dict(self, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "person_id": row["person_id"],
        "total_images": row["total_images"],
        "received_images": row["received_images"],
        "matched_images": row["matched_images"] if "matched_images" in row.keys() else 0,
        "matched": bool(row["matched"]),
        "best_similarity": row["best_similarity"],
        "created_at": row["created_at"],
        "created_at_local": self._to_local_time_label(row["created_at"]),
        "updated_at": row["updated_at"],
    }

  def _get_face_app(self) -> FaceAnalysis:
    if self._face_app is None:
      face_app = FaceAnalysis(name=self.model_name, providers=["CPUExecutionProvider"])
      face_app.prepare(ctx_id=-1, det_size=DEFAULT_DET_SIZE)
      self._face_app = face_app
    return self._face_app

  @property
  def telegram_enabled(self) -> bool:
    return bool(self.telegram_bot_token and self.telegram_chat_id)

  def _send_telegram_notification_for_event(self, event_id: int) -> None:
    if not self.telegram_enabled:
      return

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      self._debug(
          f"telegram skipped event_id={event_id}: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      event_row = connection.execute(
          """
          SELECT id, person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at
          FROM ring_events
          WHERE id = ?
          """,
          (event_id,),
      ).fetchone()
      if event_row is None:
        return

      action_row = connection.execute(
          """
          SELECT telegram_notified_at
          FROM event_actions
          WHERE event_id = ?
          """,
          (event_id,),
      ).fetchone()
      if action_row is not None and action_row["telegram_notified_at"]:
        return

      capture_row = connection.execute(
          """
          SELECT image_path, matched, similarity, detected_faces, error, sequence_index
          FROM ring_captures
          WHERE event_id = ?
          ORDER BY matched DESC, similarity DESC, sequence_index ASC
          LIMIT 1
          """,
          (event_id,),
      ).fetchone()

    event = self._event_to_dict(event_row)
    caption = self._build_telegram_caption(event, capture_row)
    image_path = self.captures_dir / capture_row["image_path"] if capture_row is not None else None

    try:
      message_id = self._telegram_send_photo(image_path, caption, event_id)
    except Exception as exc:
      self._debug(f"telegram send failed event_id={event_id}: {exc}")
      return

    now = self._utc_now()
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO event_actions (event_id, telegram_message_id, telegram_notified_at)
          VALUES (?, ?, ?)
          ON CONFLICT(event_id) DO UPDATE SET
            telegram_message_id = excluded.telegram_message_id,
            telegram_notified_at = excluded.telegram_notified_at
          """,
          (event_id, message_id, now),
      )
      connection.commit()

  def _build_telegram_caption(self, event: dict, capture_row: Optional[sqlite3.Row]) -> str:
    person_id = event["person_id"] or UNKNOWN_PERSON_ID
    recommendation = "Zulassen" if event["matched"] else "Ablehnen"
    lines = [
        f"Klingelereignis #{event['id']}",
        f"Person: {person_id}",
        f"Empfehlung: {recommendation}",
        f"Matches: {event['matched_images']}/{event['total_images']}",
        f"Zeit: {event['created_at_local']}",
    ]
    if event["best_similarity"] is not None:
      lines.append(f"Beste Confidence: {event['best_similarity']:.2f}")
    if capture_row is not None:
      if capture_row["detected_faces"] is not None:
        lines.append(f"Gesichter: {capture_row['detected_faces']}")
      if capture_row["error"]:
        lines.append(f"Hinweis: {capture_row['error']}")
    return "\n".join(lines)

  def _telegram_api_url(self, method: str) -> str:
    return f"https://api.telegram.org/bot{self.telegram_bot_token}/{method}"

  def _telegram_reply_markup(self, event_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [[
                {"text": "Reinlassen", "callback_data": f"doorbell:approve:{event_id}"},
                {"text": "Ablehnen", "callback_data": f"doorbell:deny:{event_id}"},
            ]]
        }
    )

  def _telegram_send_photo(self, image_path: Optional[Path], caption: str, event_id: int) -> int:
    if image_path is not None and image_path.exists():
      with image_path.open("rb") as image_file:
        response = requests.post(
            self._telegram_api_url("sendPhoto"),
            data={
                "chat_id": self.telegram_chat_id,
                "caption": caption,
                "reply_markup": self._telegram_reply_markup(event_id),
            },
            files={"photo": image_file},
            timeout=15,
        )
    else:
      response = requests.post(
          self._telegram_api_url("sendMessage"),
          data={
              "chat_id": self.telegram_chat_id,
              "text": caption,
              "reply_markup": self._telegram_reply_markup(event_id),
          },
          timeout=15,
      )

    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
      raise RuntimeError(f"telegram api error: {payload}")
    return int(payload["result"]["message_id"])

  def send_telegram_test_message(self) -> dict:
    if not self.telegram_enabled:
      return {"ok": False, "error": "Telegram ist nicht vollstaendig konfiguriert."}

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      return {
          "ok": False,
          "error": "Kein Internet/Uplink für Telegram-Test.",
          "network": network_status,
      }

    try:
      response = requests.post(
          self._telegram_api_url("sendMessage"),
          data={
              "chat_id": self.telegram_chat_id,
              "text": "Smart Doorbell Telegram-Test: Verbindung funktioniert.",
          },
          timeout=15,
      )
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      return {"ok": False, "error": str(exc), "network": network_status}

    if not payload.get("ok"):
      return {"ok": False, "error": f"Telegram API meldet Fehler: {payload}", "network": network_status}

    return {"ok": True, "message": "Telegram-Testnachricht wurde gesendet.", "network": network_status}

  def request_safe_shutdown(self) -> dict:
    helper_path = Path(SAFE_POWEROFF_HELPER)
    if not helper_path.exists():
      return {
          "ok": False,
          "error": (
              f"Shutdown-Helper fehlt: {SAFE_POWEROFF_HELPER}. "
              "Bitte install_autostart.sh auf dem Pi erneut ausführen."
          ),
      }

    try:
      subprocess.Popen(
          ["sudo", "-n", SAFE_POWEROFF_HELPER],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          start_new_session=True,
      )
    except Exception as exc:
      return {
          "ok": False,
          "error": (
              f"Shutdown konnte nicht gestartet werden: {exc}. "
              "Bitte prüfen, ob die sudoers-Regel durch install_autostart.sh installiert wurde."
          ),
      }

    return {"ok": True, "message": "Shutdown wurde angefordert."}

  def wifi_setup(self, command: str, **kwargs) -> dict:
    helper_path = Path(WIFI_SETUP_HELPER)
    if not helper_path.exists():
      return {
          "ok": False,
          "error": (
              f"WLAN-Helper fehlt: {WIFI_SETUP_HELPER}. "
              "Bitte install_autostart.sh auf dem Pi erneut ausführen."
          ),
      }

    args = ["sudo", "-n", WIFI_SETUP_HELPER, command]
    if command == "connect":
      args.extend(["--ssid", str(kwargs.get("ssid") or "")])
      args.extend(["--password", str(kwargs.get("password") or "")])
      if kwargs.get("name"):
        args.extend(["--name", str(kwargs["name"])])
    elif command == "activate":
      args.extend(["--name", str(kwargs.get("name") or "")])
    elif command == "priority":
      args.extend(["--name", str(kwargs.get("name") or "")])
      args.extend(["--value", str(kwargs.get("value") or "")])
    elif command not in {"status", "scan"}:
      return {"ok": False, "error": "Unbekannter WLAN-Befehl."}

    try:
      result = subprocess.run(
          args,
          check=False,
          capture_output=True,
          text=True,
          timeout=55,
      )
    except Exception as exc:
      return {"ok": False, "error": str(exc)}

    output = (result.stdout or "").strip()
    try:
      payload = json.loads(output) if output else {}
    except json.JSONDecodeError:
      payload = {"ok": False, "error": output or result.stderr or "WLAN-Helper lieferte kein JSON."}

    if result.returncode != 0 and payload.get("ok", False):
      payload["ok"] = False
    if result.returncode != 0 and "error" not in payload:
      payload["error"] = (result.stderr or output or "WLAN-Helper fehlgeschlagen.").strip()
    return payload

  def _sync_telegram_updates(self) -> None:
    if not self.telegram_enabled:
      return

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      self._debug(
          "telegram update sync skipped: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    offset = self._get_app_state("telegram_update_offset")
    params = {"timeout": 0, "allowed_updates": json.dumps(["callback_query"])}
    if offset is not None:
      params["offset"] = str(int(offset))

    try:
      response = requests.get(self._telegram_api_url("getUpdates"), params=params, timeout=15)
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      self._debug(f"telegram update sync failed: {exc}")
      return

    if not payload.get("ok"):
      self._debug(f"telegram update sync returned error payload: {payload}")
      return

    next_offset = None
    for update in payload.get("result", []):
      next_offset = int(update["update_id"]) + 1
      callback_query = update.get("callback_query")
      if callback_query is not None:
        self._process_telegram_callback(callback_query)

    if next_offset is not None:
      self._set_app_state("telegram_update_offset", str(next_offset))

  def _process_telegram_callback(self, callback_query: dict) -> None:
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}

    if str(chat.get("id", "")) != self.telegram_chat_id:
      self._answer_callback_query(callback_id, "Dieser Chat ist nicht freigeschaltet.")
      return

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "doorbell" or parts[1] not in {"approve", "deny"}:
      self._answer_callback_query(callback_id, "Unbekannter Befehl.")
      return

    try:
      event_id = int(parts[2])
    except ValueError:
      self._answer_callback_query(callback_id, "Ungültige Ereignis-ID.")
      return

    decision = parts[1]
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      existing = connection.execute(
          "SELECT decision FROM event_actions WHERE event_id = ?",
          (event_id,),
      ).fetchone()
      if existing is not None and existing["decision"]:
        self._answer_callback_query(callback_id, "Dieses Klingeln wurde schon entschieden.")
        return

      connection.execute(
          """
          INSERT INTO event_actions (event_id, decision, decision_source, decided_at)
          VALUES (?, ?, ?, ?)
          ON CONFLICT(event_id) DO UPDATE SET
            decision = excluded.decision,
            decision_source = excluded.decision_source,
            decided_at = excluded.decided_at
          """,
          (event_id, decision, "telegram", now),
      )
      connection.commit()

    self._answer_callback_query(
        callback_id,
        "Zutritt freigegeben." if decision == "approve" else "Zutritt abgelehnt.",
    )

  def _answer_callback_query(self, callback_id: Optional[str], text: str) -> None:
    if not callback_id or not self.telegram_enabled:
      return
    try:
      response = requests.post(
          self._telegram_api_url("answerCallbackQuery"),
          data={"callback_query_id": callback_id, "text": text},
          timeout=15,
      )
      response.raise_for_status()
    except Exception as exc:
      self._debug(f"telegram callback answer failed: {exc}")

  def _get_app_state(self, key: str) -> Optional[str]:
    with sqlite3.connect(self.db_path) as connection:
      row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])

  def _set_app_state(self, key: str, value: str) -> None:
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO app_state (key, value)
          VALUES (?, ?)
          ON CONFLICT(key) DO UPDATE SET value = excluded.value
          """,
          (key, value),
      )
      connection.commit()

  def _extract_primary_face(self, image: np.ndarray):
    faces = self._get_face_app().get(image)
    if len(faces) == 0:
      raise ValueError("Kein Gesicht erkannt.")
    if len(faces) > 1:
      faces.sort(key=self._face_area, reverse=True)
    return faces[0]

  @staticmethod
  def _face_area(face) -> float:
    bbox = face.bbox
    return float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))

  @staticmethod
  def _decode_image(image_bytes: bytes) -> np.ndarray:
    image_buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_buffer, cv2.IMREAD_COLOR)
    if image is None:
      raise ValueError("Bilddaten konnten nicht dekodiert werden.")
    return image

  @staticmethod
  def _get_normalized_embedding(face) -> np.ndarray:
    embedding = getattr(face, "normed_embedding", None)
    if embedding is None:
      embedding = face.embedding
      norm = np.linalg.norm(embedding)
      if norm == 0:
        raise ValueError("Embedding hat Norm 0.")
      embedding = embedding / norm
    return np.asarray(embedding, dtype=np.float32)

  @staticmethod
  def _serialize_embedding(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()

  @staticmethod
  def _deserialize_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)

  @staticmethod
  def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

  @staticmethod
  def _to_local_time_label(utc_iso: str) -> str:
    return datetime.fromisoformat(utc_iso).astimezone().strftime("%d.%m.%Y %H:%M:%S")

  @staticmethod
  def _local_time_label() -> str:
    return datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")

  def _load_reference_embeddings(self, person_id: str) -> np.ndarray:
    with sqlite3.connect(self.db_path) as connection:
      rows = connection.execute(
          "SELECT embedding FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      ).fetchall()

    if not rows:
      return np.empty((0, 512), dtype=np.float32)

    embeddings = [self._deserialize_embedding(row[0]) for row in rows]
    return np.vstack(embeddings)

  def _reference_count(self, person_id: str) -> int:
    with sqlite3.connect(self.db_path) as connection:
      return connection.execute(
          "SELECT COUNT(*) FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      ).fetchone()[0]

  def _log_verification(self, result: VerificationResult) -> None:
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO verification_logs (
            person_id,
            matched,
            similarity,
            threshold,
            reference_count,
            detected_faces,
            error,
            created_at
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
              result.person_id,
              int(result.matched),
              result.similarity,
              result.threshold,
              result.reference_count,
              result.detected_faces,
              result.error,
              self._utc_now(),
          ),
      )
      connection.commit()

  @staticmethod
  def _ensure_column(
      connection: sqlite3.Connection,
      table_name: str,
      column_name: str,
      column_definition: str,
  ) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_names = {column[1] for column in columns}
    if column_name not in existing_names:
      connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def create_app(service: FaceVerifierService) -> Flask:
  app = Flask(__name__)

  @app.get("/")
  def index():
    return redirect("/setup")

  @app.get("/setup")
  def setup_page():
    response = make_response(render_template("setup.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

  @app.get("/health")
  def health():
    network_status = service.network_status()
    return jsonify(
        {
            "status": "ok" if network_status["internet_ok"] else "degraded",
            "model": service.model_name,
            "threshold": service.similarity_threshold,
            "db_path": str(service.db_path),
            "telegram_enabled": service.telegram_enabled,
            "setup_complete": service.setup_complete,
            "config": service.config_status(),
            "network": network_status,
        }
    )

  @app.get("/api/network-status")
  def network_status():
    status = service.network_status()
    return jsonify({"ok": status["internet_ok"], **status}), (200 if status["internet_ok"] else 503)

  @app.get("/api/persons")
  def list_people():
    return jsonify({"people": service.list_people()})

  @app.post("/api/persons/<path:person_id>/active")
  def set_person_active(person_id: str):
    payload = request.get_json(silent=True) or request.form
    active_value = payload.get("active")
    if isinstance(active_value, bool):
      active = active_value
    else:
      active = str(active_value).lower() in {"1", "true", "yes", "on", "active"}

    try:
      result = service.set_person_active(person_id=person_id, active=active)
      return jsonify({"ok": True, **result})
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.delete("/api/persons/<path:person_id>")
  def delete_person(person_id: str):
    result = service.delete_person(person_id=person_id)
    if result["deleted_references"] == 0:
      return jsonify({"ok": False, "error": "Person oder Referenzbilder nicht gefunden.", **result}), 404
    return jsonify({"ok": True, **result})

  @app.post("/api/enroll-session/discard")
  def discard_enroll_session():
    payload = request.get_json(silent=True) or request.form
    try:
      result = service.discard_enroll_session(
          person_id=str(payload.get("person_id") or ""),
          session_id=str(payload.get("session_id") or ""),
      )
      return jsonify({"ok": True, **result})
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.get("/api/config")
  def config_status():
    return jsonify(service.config_status())

  @app.post("/api/config")
  def save_config():
    payload = request.get_json(silent=True) or request.form
    telegram_bot_token = payload.get("telegram_bot_token")
    telegram_chat_id = payload.get("telegram_chat_id")
    esp_snapshot_url = payload.get("esp_snapshot_url")
    similarity_threshold = payload.get("similarity_threshold")

    try:
      status = service.update_config(
          telegram_bot_token=telegram_bot_token,
          telegram_chat_id=telegram_chat_id,
          esp_snapshot_url=esp_snapshot_url,
          similarity_threshold=similarity_threshold,
      )
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, **status})

  @app.post("/api/test-telegram")
  def test_telegram():
    payload = service.send_telegram_test_message()
    return jsonify(payload), (200 if payload.get("ok") else 400)

  @app.post("/api/system/shutdown")
  def system_shutdown():
    payload = service.request_safe_shutdown()
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.get("/api/wifi/status")
  def wifi_status():
    payload = service.wifi_setup("status")
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.post("/api/wifi/scan")
  def wifi_scan():
    payload = service.wifi_setup("scan")
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.post("/api/wifi/connect")
  def wifi_connect():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup(
        "connect",
        ssid=payload.get("ssid"),
        password=payload.get("password"),
        name=payload.get("name"),
    )
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.post("/api/wifi/activate")
  def wifi_activate():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup("activate", name=payload.get("name"))
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.post("/api/wifi/priority")
  def wifi_priority():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup("priority", name=payload.get("name"), value=payload.get("priority"))
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.get("/api/live-snapshot")
  def live_snapshot():
    try:
      image_bytes = service.fetch_live_snapshot()
    except (urlerror.URLError, TimeoutError, OSError) as exc:
      return jsonify({"ok": False, "error": str(exc)}), 502

    response = make_response(image_bytes)
    response.headers["Content-Type"] = "image/jpeg"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

  @app.get("/captures/<path:filename>")
  def capture_file(filename: str):
    return send_from_directory(service.captures_dir, filename)

  @app.post("/api/enroll")
  def enroll():
    service._debug("enroll request received")
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    note = request.form.get("note")
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    filename = request.files["image"].filename if "image" in request.files else "raw-request-body"
    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

    service._debug(f"enroll start person_id={person_id} filename={filename} bytes={len(image_bytes)}")
    try:
      payload = service.enroll_embedding(person_id=person_id, image_bytes=image_bytes, note=note)
      service._debug(f"enroll ok person_id={person_id} filename={filename} reference_count={payload['reference_count']}")
      return jsonify({"ok": True, **payload}), 201
    except Exception as exc:
      service._debug(f"enroll failed person_id={person_id} filename={filename}: {exc}")
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.post("/api/enroll-batch")
  def enroll_batch():
    service._debug("enroll_batch request received")
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    note = request.form.get("note")
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    uploaded_files = request.files.getlist("images") or request.files.getlist("image")
    if not uploaded_files:
      return jsonify({"ok": False, "error": "Es wurden keine Bilder übergeben."}), 400

    service._debug(f"enroll_batch start person_id={person_id} images={len(uploaded_files)}")
    results = []
    success_count = 0
    for index, uploaded_file in enumerate(uploaded_files, start=1):
      filename = uploaded_file.filename or f"image_{index}"
      image_bytes = uploaded_file.read()
      service._debug(f"enroll_batch image {index}/{len(uploaded_files)} filename={filename} bytes={len(image_bytes)}")
      if not image_bytes:
        results.append({"filename": filename, "ok": False, "error": "Datei ist leer."})
        continue

      try:
        payload = service.enroll_embedding(
            person_id=person_id,
            image_bytes=image_bytes,
            note=note or f"setup-upload:{filename}",
        )
        success_count += 1
        service._debug(f"enroll_batch image {index}/{len(uploaded_files)} ok reference_count={payload['reference_count']}")
        results.append({"filename": filename, "ok": True, **payload})
      except Exception as exc:
        service._debug(f"enroll_batch image {index}/{len(uploaded_files)} failed: {exc}")
        results.append({"filename": filename, "ok": False, "error": str(exc)})

    service._debug(
        f"enroll_batch done person_id={person_id} successful={success_count}/{len(uploaded_files)}"
    )
    return jsonify(
        {
            "ok": success_count > 0,
            "person_id": person_id,
            "received_images": len(uploaded_files),
            "successful_images": success_count,
            "failed_images": len(uploaded_files) - success_count,
            "results": results,
        }
    ), (201 if success_count > 0 else 400)

  @app.post("/api/enroll-from-esp")
  def enroll_from_esp():
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    count_value = request.form.get("count") or request.args.get("count") or "1"
    step = request.form.get("step") or request.args.get("step")
    instruction = request.form.get("instruction") or request.args.get("instruction")
    session_id = request.form.get("session_id") or request.args.get("session_id")
    note_parts = ["esp-camera"]
    if session_id:
      note_parts.append(f"session:{session_id}")
    if step:
      note_parts.append(f"step:{step}")
    if instruction:
      note_parts.append(instruction)
    try:
      count = int(count_value)
      payload = service.enroll_from_esp(
          person_id=person_id or "",
          count=count,
          note=" | ".join(note_parts),
          session_id=session_id,
      )
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(payload), (201 if payload["ok"] else 400)

  @app.post("/api/verify")
  def verify():
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
    )

    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

    threshold_value = request.form.get("threshold") or request.args.get("threshold")
    threshold = float(threshold_value) if threshold_value else None
    result = service.verify(person_id=person_id, image_bytes=image_bytes, threshold=threshold)

    status_code = 200 if result.error is None else 422
    return (
        jsonify(
            {
                "ok": result.error is None,
                "person_id": result.person_id,
                "matched": result.matched,
                "similarity": result.similarity,
                "threshold": result.threshold,
                "reference_count": result.reference_count,
                "detected_faces": result.detected_faces,
                "error": result.error,
            }
        ),
        status_code,
    )

  @app.post("/api/ring-capture")
  def ring_capture():
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
    )
    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

    sequence_index = int(request.form.get("sequence") or request.args.get("sequence") or "1")
    total_images = int(request.form.get("total") or request.args.get("total") or "1")
    event_id_value = request.form.get("event_id") or request.args.get("event_id")
    event_id = int(event_id_value) if event_id_value else None
    threshold_value = request.form.get("threshold") or request.args.get("threshold")
    threshold = float(threshold_value) if threshold_value else None

    try:
      payload = service.handle_ring_capture(
          person_id=person_id,
          image_bytes=image_bytes,
          sequence_index=sequence_index,
          total_images=total_images,
          event_id=event_id,
          threshold=threshold,
      )
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

    status_code = 200 if payload["ok"] else 422
    return jsonify(payload), status_code

  @app.post("/api/esp-log")
  def esp_log():
    mac = request.headers.get("X-ESP-MAC", "unknown")
    message = request.get_data(as_text=True).strip()
    if message:
      print(f"[ESP {mac}] {message}", flush=True)
    return jsonify({"ok": True})

  @app.get("/api/ring-decision")
  def ring_decision():
    event_id_value = request.args.get("event_id")
    if not event_id_value:
      return jsonify({"ok": False, "error": "event_id fehlt."}), 400

    try:
      event_id = int(event_id_value)
    except ValueError:
      return jsonify({"ok": False, "error": "event_id ist ungültig."}), 400

    return jsonify(service.get_event_decision(event_id))

  def _read_image_bytes() -> Optional[bytes]:
    if "image" in request.files:
      return request.files["image"].read()

    if request.data:
      return request.data

    return None

  return app


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Face verification prototype for Raspberry Pi 4.")
  parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Pfad zur SQLite-Datenbank.")
  parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH), help="Pfad zur lokalen Konfigurationsdatei.")
  parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="InsightFace-Modellname.")
  parser.add_argument(
      "--threshold",
      type=float,
      default=DEFAULT_SIMILARITY_THRESHOLD,
      help="Cosine-Similarity-Schwelle für match/no_match.",
  )

  subparsers = parser.add_subparsers(dest="command", required=True)

  subparsers.add_parser("init-db", help="SQLite-Schema anlegen.")

  serve_parser = subparsers.add_parser("serve", help="HTTP-Server starten.")
  serve_parser.add_argument("--host", default="0.0.0.0")
  serve_parser.add_argument("--port", type=int, default=8000)

  enroll_parser = subparsers.add_parser("enroll-image", help="Ein Bild als Referenz-Embedding speichern.")
  enroll_parser.add_argument("--person-id", required=True)
  enroll_parser.add_argument("--image", required=True)
  enroll_parser.add_argument("--note")

  verify_parser = subparsers.add_parser("verify-image", help="Ein Bild lokal gegen Referenzen pruefen.")
  verify_parser.add_argument("--person-id", help="Optional: nur gegen diese Person pruefen.")
  verify_parser.add_argument("--image", required=True)

  return parser.parse_args()


def main() -> None:
  args = parse_args()
  service = FaceVerifierService(
      db_path=Path(args.db_path),
      config_path=Path(args.config_path),
      model_name=args.model_name,
      similarity_threshold=args.threshold,
  )
  service.init_db()

  if args.command == "init-db":
    print(json.dumps({"ok": True, "db_path": str(service.db_path)}))
    return

  if args.command == "enroll-image":
    image_bytes = Path(args.image).read_bytes()
    payload = service.enroll_embedding(person_id=args.person_id, image_bytes=image_bytes, note=args.note)
    print(json.dumps({"ok": True, **payload}, ensure_ascii=True))
    return

  if args.command == "verify-image":
    image_bytes = Path(args.image).read_bytes()
    result = service.verify(person_id=args.person_id, image_bytes=image_bytes)
    print(
        json.dumps(
            {
                "ok": result.error is None,
                "person_id": result.person_id,
                "matched": result.matched,
                "similarity": result.similarity,
                "threshold": result.threshold,
                "reference_count": result.reference_count,
                "detected_faces": result.detected_faces,
                "error": result.error,
            },
            ensure_ascii=True,
        )
    )
    return

  if args.command == "serve":
    app = create_app(service)
    app.run(host=args.host, port=args.port)
    return

  raise ValueError(f"Unbekanntes Kommando: {args.command}")


if __name__ == "__main__":
  main()
