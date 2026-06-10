"""Bild-Dekodierung und Embedding-Vektor-Helfer.

Reine Funktionen ohne Zustand – sie hängen weder an der Datenbank noch am
Dienst. Die eigentliche Gesichtserkennung liegt in ``doorbell.recognition``.
"""

import cv2
import numpy as np


def decode_image(image_bytes: bytes) -> np.ndarray:
  image_buffer = np.frombuffer(image_bytes, dtype=np.uint8)
  image = cv2.imdecode(image_buffer, cv2.IMREAD_COLOR)
  if image is None:
    raise ValueError("Bilddaten konnten nicht dekodiert werden.")
  return image


def face_area(face) -> float:
  bbox = face.bbox
  return float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))


def normalized_embedding(face) -> np.ndarray:
  embedding = getattr(face, "normed_embedding", None)
  if embedding is None:
    embedding = face.embedding
    norm = np.linalg.norm(embedding)
    if norm == 0:
      raise ValueError("Embedding hat Norm 0.")
    embedding = embedding / norm
  return np.asarray(embedding, dtype=np.float32)


def serialize_embedding(embedding: np.ndarray) -> bytes:
  return embedding.astype(np.float32).tobytes()


def deserialize_embedding(blob: bytes) -> np.ndarray:
  return np.frombuffer(blob, dtype=np.float32)
