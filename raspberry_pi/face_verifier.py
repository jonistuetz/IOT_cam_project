import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from flask import Flask, jsonify, request
from insightface.app import FaceAnalysis


DEFAULT_DB_PATH = Path(__file__).with_name("face_verification.db")
DEFAULT_MODEL_NAME = "buffalo_sc"
DEFAULT_SIMILARITY_THRESHOLD = 0.42
DEFAULT_DET_SIZE = (640, 640)


@dataclass
class VerificationResult:
  person_id: str
  matched: bool
  similarity: Optional[float]
  threshold: float
  reference_count: int
  detected_faces: int
  error: Optional[str] = None


class FaceVerifierService:
  def __init__(
      self,
      db_path: Path,
      model_name: str = DEFAULT_MODEL_NAME,
      similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
  ) -> None:
    self.db_path = Path(db_path)
    self.model_name = model_name
    self.similarity_threshold = similarity_threshold
    self._face_app: Optional[FaceAnalysis] = None

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
      connection.commit()

  def enroll_embedding(self, person_id: str, image_bytes: bytes, note: Optional[str] = None) -> dict:
    image = self._decode_image(image_bytes)
    face = self._extract_primary_face(image)
    embedding = self._get_normalized_embedding(face)

    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO reference_embeddings (person_id, embedding, note, created_at)
          VALUES (?, ?, ?, ?)
          """,
          (person_id, self._serialize_embedding(embedding), note, self._utc_now()),
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

  def verify(self, person_id: str, image_bytes: bytes, threshold: Optional[float] = None) -> VerificationResult:
    similarity_threshold = self.similarity_threshold if threshold is None else threshold

    try:
      image = self._decode_image(image_bytes)
      faces = self._get_face_app().get(image)
      if len(faces) == 0:
        result = VerificationResult(
            person_id=person_id,
            matched=False,
            similarity=None,
            threshold=similarity_threshold,
            reference_count=self._reference_count(person_id),
            detected_faces=0,
            error="Kein Gesicht erkannt.",
        )
        self._log_verification(result)
        return result

      if len(faces) > 1:
        faces.sort(key=self._face_area, reverse=True)

      probe_embedding = self._get_normalized_embedding(faces[0])
      references = self._load_reference_embeddings(person_id)

      if references.size == 0:
        result = VerificationResult(
            person_id=person_id,
            matched=False,
            similarity=None,
            threshold=similarity_threshold,
            reference_count=0,
            detected_faces=len(faces),
            error="Keine Referenz-Embeddings fuer diese Person gespeichert.",
        )
        self._log_verification(result)
        return result

      similarities = np.dot(references, probe_embedding)
      best_similarity = float(np.max(similarities))
      matched = best_similarity >= similarity_threshold
      result = VerificationResult(
          person_id=person_id,
          matched=matched,
          similarity=best_similarity,
          threshold=similarity_threshold,
          reference_count=len(references),
          detected_faces=len(faces),
      )
      self._log_verification(result)
      return result
    except Exception as exc:  # pragma: no cover - defensive logging for prototype runtime
      result = VerificationResult(
          person_id=person_id,
          matched=False,
          similarity=None,
          threshold=similarity_threshold,
          reference_count=self._reference_count(person_id),
          detected_faces=0,
          error=str(exc),
      )
      self._log_verification(result)
      return result

  def list_people(self) -> list[dict]:
    with sqlite3.connect(self.db_path) as connection:
      rows = connection.execute(
          """
          SELECT person_id, COUNT(*) AS reference_count, MAX(created_at) AS updated_at
          FROM reference_embeddings
          GROUP BY person_id
          ORDER BY person_id
          """
      ).fetchall()

    return [
        {
            "person_id": row[0],
            "reference_count": row[1],
            "updated_at": row[2],
        }
        for row in rows
    ]

  def _get_face_app(self) -> FaceAnalysis:
    if self._face_app is None:
      face_app = FaceAnalysis(name=self.model_name, providers=["CPUExecutionProvider"])
      face_app.prepare(ctx_id=-1, det_size=DEFAULT_DET_SIZE)
      self._face_app = face_app
    return self._face_app

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


def create_app(service: FaceVerifierService) -> Flask:
  app = Flask(__name__)

  @app.get("/health")
  def health():
    return jsonify(
        {
            "status": "ok",
            "model": service.model_name,
            "threshold": service.similarity_threshold,
            "db_path": str(service.db_path),
        }
    )

  @app.get("/api/persons")
  def list_people():
    return jsonify({"people": service.list_people()})

  @app.post("/api/enroll")
  def enroll():
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    note = request.form.get("note")
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild uebergeben."}), 400

    try:
      payload = service.enroll_embedding(person_id=person_id, image_bytes=image_bytes, note=note)
      return jsonify({"ok": True, **payload}), 201
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.post("/api/verify")
  def verify():
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
    )
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild uebergeben."}), 400

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
  parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="InsightFace-Modellname.")
  parser.add_argument(
      "--threshold",
      type=float,
      default=DEFAULT_SIMILARITY_THRESHOLD,
      help="Cosine-Similarity-Schwelle fuer match/no_match.",
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
  verify_parser.add_argument("--person-id", required=True)
  verify_parser.add_argument("--image", required=True)

  return parser.parse_args()


def main() -> None:
  args = parse_args()
  service = FaceVerifierService(
      db_path=Path(args.db_path),
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
