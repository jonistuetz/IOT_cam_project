"""Verifikationslogik: Probe-Embedding gegen gespeicherte Referenzen prüfen.

Als Mixin gestaltet – die Methoden greifen über ``self`` auf die vom
``FaceVerifierService`` bereitgestellten Helfer (Bild-/Embedding-Funktionen,
Datenbankpfad, Logging) zu.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .constants import UNKNOWN_PERSON_ID


@dataclass
class VerificationResult:
  person_id: str
  matched: bool
  similarity: Optional[float]
  threshold: float
  reference_count: int
  detected_faces: int
  error: Optional[str] = None


class VerificationMixin:
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
