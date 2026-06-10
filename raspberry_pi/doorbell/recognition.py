"""Gesichtserkennung auf Basis von InsightFace.

``FaceRecognizer`` kapselt das (lazy initialisierte) Modell und die Detektion.
Die reine Vektor-/Bildmathematik (Dekodieren, Normalisieren, Fläche) liegt in
``doorbell.utils.images``.
"""

from insightface.app import FaceAnalysis

from .constants import DEFAULT_DET_SIZE, DEFAULT_MODEL_NAME
from .utils.images import face_area


class FaceRecognizer:
  def __init__(self, model_name: str = DEFAULT_MODEL_NAME) -> None:
    self.model_name = model_name
    self._face_app = None

  @property
  def app(self) -> FaceAnalysis:
    if self._face_app is None:
      face_app = FaceAnalysis(name=self.model_name, providers=["CPUExecutionProvider"])
      face_app.prepare(ctx_id=-1, det_size=DEFAULT_DET_SIZE)
      self._face_app = face_app
    return self._face_app

  def get(self, image):
    """Erkenne alle Gesichter im Bild (Liste, ggf. leer)."""
    return self.app.get(image)

  def extract_primary_face(self, image):
    faces = self.get(image)
    if len(faces) == 0:
      raise ValueError("Kein Gesicht erkannt.")
    if len(faces) > 1:
      faces.sort(key=face_area, reverse=True)
    return faces[0]
