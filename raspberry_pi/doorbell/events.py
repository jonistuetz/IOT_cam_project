"""Klingelereignisse: Bildserie auswerten, Status führen, Entscheidung abfragen.

Als Mixin gestaltet; nutzt die Verifikation (``self.verify``), den Telegram-
Versand und den Datenbankpfad des Dienstes.
"""

import sqlite3
from datetime import datetime, timezone

from .constants import DEFAULT_REQUIRED_MATCHES_FOR_ACCESS


class EventsMixin:
  def handle_ring_capture(
      self,
      person_id,
      image_bytes: bytes,
      sequence_index: int,
      total_images: int,
      event_id=None,
      threshold=None,
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
        # Das erste Bild eines Bursts legt das Klingelereignis an. Die weiteren
        # Bilder kommen mit derselben event_id zurück und aktualisieren Zähler
        # und beste Ähnlichkeit.
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
      # Mehrheitsähnliche Entscheidung: Bei drei Bildern reichen zwei Treffer.
      # Das reduziert Fehlentscheidungen durch ein einzelnes schlechtes Frame.
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
      # Erst nach dem letzten Bild wird Telegram informiert, damit die Nachricht
      # die aggregierte Empfehlung und das beste gespeicherte Bild enthält.
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
    # Der ESP pollt diesen lokalen Endpunkt. Vor dem Datenbank-Lookup werden
    # Telegram-Updates synchronisiert, sodass neue Button-Klicks ohne separaten
    # Hintergrundworker sichtbar werden.
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
