"""Anlernen von Referenzbildern (lokal hochgeladen oder von der ESP-Kamera).

Als Mixin gestaltet; nutzt über ``self`` die Bild-/Embedding-Helfer, den
Datenbankpfad, das Captures-Verzeichnis und die ESP-Snapshot-URL des Dienstes.
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from urllib import request as urlrequest


class EnrollmentMixin:
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
