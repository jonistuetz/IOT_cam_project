#!/usr/bin/env python3
"""Einstiegspunkt des Smart-Doorbell-Dienstes.

Kommandos:
  init-db        SQLite-Schema anlegen.
  serve          HTTP-Server starten (Standard im Autostart).
  enroll-image   Ein Bild als Referenz-Embedding speichern.
  verify-image   Ein Bild lokal gegen Referenzen prüfen.

Die eigentliche Logik liegt im Paket ``doorbell``.
"""

import argparse
import json
from pathlib import Path

from doorbell.constants import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_MODEL_NAME,
    DEFAULT_SIMILARITY_THRESHOLD,
)
from doorbell.service import FaceVerifierService
from doorbell.web.app import create_app


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Gesichtsverifikation für die smarte Türklingel auf dem Raspberry Pi 4.")
  parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Pfad zur SQLite-Datenbank.")
  parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH), help="Pfad zur lokalen Konfigurationsdatei.")
  parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="InsightFace-Modellname.")
  parser.add_argument(
      "--threshold",
      type=float,
      default=DEFAULT_SIMILARITY_THRESHOLD,
      help="Cosine-Similarity-Schwelle für Treffer/Nicht-Treffer.",
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
