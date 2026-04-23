import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import cv2
import numpy as np
from flask import Flask, jsonify, make_response, request, send_from_directory
from insightface.app import FaceAnalysis


DEFAULT_DB_PATH = Path(__file__).with_name("face_verification.db")
DEFAULT_CAPTURES_DIR = Path(__file__).with_name("captures")
DEFAULT_MODEL_NAME = "buffalo_sc"
DEFAULT_SIMILARITY_THRESHOLD = 0.42
DEFAULT_DET_SIZE = (640, 640)
DEFAULT_PERSON_ID = "jonathan"
DEFAULT_ESP_SNAPSHOT_URL = "http://10.42.0.172/snapshot"

DEFAULT_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Smart Doorbell Dashboard v2</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f1ede5;
      --card: #fffaf3;
      --text: #1f2933;
      --accent: #b45309;
      --ok: #166534;
      --bad: #991b1b;
      --muted: #5b6572;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: linear-gradient(180deg, #f6f1e8 0%, #ece2d1 100%);
      color: var(--text);
      padding: 24px;
    }
    .layout {
      width: min(1200px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 20px;
    }
    .hero, .panel {
      background: var(--card);
      border-radius: 22px;
      padding: 20px;
      box-shadow: 0 20px 45px rgba(69, 48, 18, 0.12);
    }
    .hero h1, .panel h2 {
      margin: 0 0 12px;
    }
    .meta {
      color: var(--muted);
      line-height: 1.5;
    }
    .status {
      display: inline-block;
      border-radius: 999px;
      padding: 6px 12px;
      font-weight: 700;
      margin-top: 10px;
    }
    .status.ok { background: rgba(22, 101, 52, 0.12); color: var(--ok); }
    .status.bad { background: rgba(153, 27, 27, 0.12); color: var(--bad); }
    .shots {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-top: 16px;
    }
    .shot {
      background: #f8f3ea;
      border-radius: 18px;
      padding: 12px;
    }
    .shot img {
      width: 100%;
      border-radius: 14px;
      background: #111;
      aspect-ratio: 4 / 3;
      object-fit: cover;
    }
    .shot .label {
      margin-top: 10px;
      font-size: 14px;
      line-height: 1.45;
    }
    .events {
      display: grid;
      gap: 12px;
      margin-top: 12px;
    }
    .event {
      background: #f8f3ea;
      border-radius: 16px;
      padding: 14px;
    }
    .event strong {
      display: block;
      margin-bottom: 6px;
    }
    .timestamp {
      color: var(--muted);
      font-size: 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 20px;
    }
    .live {
      display: grid;
      gap: 12px;
    }
    .live img {
      width: 100%;
      border-radius: 18px;
      background: #111;
      aspect-ratio: 4 / 3;
      object-fit: cover;
    }
    .toolbar {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .toolbar button, .toolbar select, .toolbar input {
      border-radius: 12px;
      border: 1px solid rgba(31, 41, 51, 0.12);
      padding: 10px 12px;
      font: inherit;
    }
    .toolbar button {
      background: var(--accent);
      color: white;
      border: 0;
      cursor: pointer;
      font-weight: 700;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="layout">
    <section class="hero">
      <h1>Smart Doorbell Dashboard</h1>
      <div class="meta">Letzte Klingelereignisse, aktuelle Burst-Snapshots und Verifikation vom Raspberry Pi. <code>UI-Version: v2</code></div>
      <div id="systemStatus" class="status">Lade Status...</div>
      <div class="toolbar">
        <button id="refreshLiveButton" type="button">Livebild aktualisieren</button>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Livebild</h2>
        <div class="live">
          <img id="liveImage" src="/api/live-snapshot?t=0" alt="Aktuelles Livebild">
          <div class="meta">Der Raspberry Pi holt dieses Bild direkt vom ESP ab.</div>
        </div>
      </div>

      <div class="panel">
        <h2>Aktuelles Klingelereignis</h2>
        <div id="latestSummary" class="meta">Noch keine Daten.</div>
        <div id="latestShots" class="shots"></div>
      </div>
    </section>

    <section class="panel">
      <h2>Verlauf</h2>
      <div class="toolbar">
        <label>Person
          <select id="personFilter">
            <option value="">Alle</option>
          </select>
        </label>
        <label>Tag
          <input id="dayFilter" type="date">
        </label>
        <label>Status
          <select id="matchedFilter">
            <option value="">Alle</option>
            <option value="match">Nur Match</option>
            <option value="no-match">Nur kein Match</option>
          </select>
        </label>
      </div>
      <div id="events" class="events"></div>
    </section>
  </main>

  <script>
    const liveImage = document.getElementById("liveImage");
    const refreshLiveButton = document.getElementById("refreshLiveButton");
    const personFilter = document.getElementById("personFilter");
    const dayFilter = document.getElementById("dayFilter");
    const matchedFilter = document.getElementById("matchedFilter");

    function escapeHtml(value) {
      return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;");
    }

    function fmt(v) {
      return v == null ? "n/a" : Number(v).toFixed(3);
    }

    function refreshLiveImage() {
      liveImage.src = "/api/live-snapshot?t=" + Date.now();
    }

    function activeFilters() {
      const params = new URLSearchParams();
      if (personFilter.value) params.set("person_id", personFilter.value);
      if (dayFilter.value) params.set("day", dayFilter.value);
      if (matchedFilter.value) params.set("matched", matchedFilter.value);
      return params;
    }

    function updatePersonFilter(people, selectedValue) {
      const options = ['<option value="">Alle</option>']
          .concat((people || []).map((personId) => `<option value="${escapeHtml(personId)}">${escapeHtml(personId)}</option>`));
      personFilter.innerHTML = options.join("");
      personFilter.value = selectedValue || "";
    }

    async function loadDashboard() {
      const params = activeFilters();
      const response = await fetch("/api/dashboard?" + params.toString());
      const data = await response.json();

      const statusNode = document.getElementById("systemStatus");
      statusNode.textContent = data.system_status;
      statusNode.className = "status " + (data.latest_event && data.latest_event.matched ? "ok" : "bad");
      updatePersonFilter(data.available_people, params.get("person_id"));

      const latestSummary = document.getElementById("latestSummary");
      const latestShots = document.getElementById("latestShots");
      const events = document.getElementById("events");

      if (!data.latest_event) {
        latestSummary.innerHTML = "Noch kein Klingelereignis empfangen.";
        latestShots.innerHTML = "";
      } else {
        const event = data.latest_event;
        latestSummary.innerHTML =
            "<strong>" + escapeHtml(event.matched ? "Zugang erlaubt" : "Zugang nicht erlaubt") + "</strong><br>" +
            "Zeit: " + escapeHtml(event.created_at_local) + "<br>" +
            "Beste Confidence: " + escapeHtml(fmt(event.best_similarity)) + "<br>" +
            "Matches im Burst: " + escapeHtml(event.matched_images) + " / " + escapeHtml(event.total_images) + "<br>" +
            "Empfangene Bilder: " + escapeHtml(event.received_images) + " / " + escapeHtml(event.total_images) + "<br>" +
            "Person: " + escapeHtml(event.person_id);

        latestShots.innerHTML = (data.latest_captures || []).map((capture) => {
          const state = capture.matched ? "Match" : "Kein Match";
          let reason = "Match erkannt";
          if (!capture.matched) {
            reason = capture.error || (capture.detected_faces === 0 ? "Kein Gesicht erkannt" : "Score unter Schwellwert");
          }
          return `
            <article class="shot">
              <img src="${escapeHtml(capture.image_url)}?t=${Date.now()}" alt="Snapshot ${escapeHtml(capture.sequence_index)}">
              <div class="label">
                <strong>Bild ${escapeHtml(capture.sequence_index)}: ${escapeHtml(state)}</strong><br>
                Confidence: ${escapeHtml(fmt(capture.similarity))}<br>
                Gesichter: ${escapeHtml(capture.detected_faces)}<br>
                Grund: ${escapeHtml(reason)}
              </div>
            </article>
          `;
        }).join("");
      }

      events.innerHTML = (data.recent_events || []).map((event) => `
        <article class="event">
          <strong>${escapeHtml(event.matched ? "Zugang erlaubt" : "Zugang nicht erlaubt")}</strong>
          <div class="timestamp">${escapeHtml(event.created_at_local)}</div>
          <div>Person: ${escapeHtml(event.person_id)}</div>
          <div>Matches im Burst: ${escapeHtml(event.matched_images)} / ${escapeHtml(event.total_images)}</div>
          <div>Bilder: ${escapeHtml(event.received_images)} / ${escapeHtml(event.total_images)}</div>
          <div>Beste Confidence: ${escapeHtml(fmt(event.best_similarity))}</div>
        </article>
      `).join("") || '<div class="meta">Noch keine Ereignisse vorhanden.</div>';
    }

    refreshLiveButton.addEventListener("click", refreshLiveImage);
    personFilter.addEventListener("change", loadDashboard);
    dayFilter.addEventListener("change", loadDashboard);
    matchedFilter.addEventListener("change", loadDashboard);

    loadDashboard();
    refreshLiveImage();
    setInterval(loadDashboard, 3000);
  </script>
</body>
</html>
"""


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
    self.captures_dir = DEFAULT_CAPTURES_DIR
    self.model_name = model_name
    self.similarity_threshold = similarity_threshold
    self._face_app: Optional[FaceAnalysis] = None
    self.captures_dir.mkdir(parents=True, exist_ok=True)

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

  def list_person_ids(self) -> list[str]:
    return [entry["person_id"] for entry in self.list_people()]

  def handle_ring_capture(
      self,
      person_id: str,
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

    result = self.verify(person_id=person_id, image_bytes=image_bytes, threshold=threshold)
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      if event_id is None:
        cursor = connection.execute(
            """
            INSERT INTO ring_events (person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at)
            VALUES (?, ?, 0, 0, 0, NULL, ?, ?)
            """,
            (person_id, total_images, now, now),
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
      event_matched = bool(event_row[2]) or result.matched
      current_best = event_row[3]
      best_similarity = current_best
      if result.similarity is not None and (best_similarity is None or result.similarity > best_similarity):
        best_similarity = result.similarity

      connection.execute(
          """
          UPDATE ring_events
          SET received_images = ?, matched_images = ?, matched = ?, best_similarity = ?, updated_at = ?
          WHERE id = ?
          """,
          (received_images, matched_images, int(event_matched), best_similarity, now, event_id),
      )
      connection.commit()

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
        "event_complete": received_images >= total_images,
        "matched_images": matched_images,
        "overall_matched": event_matched,
        "overall_best_similarity": best_similarity,
        "image_url": f"/captures/{image_filename}",
    }

  def dashboard_data(
      self,
      person_id: Optional[str] = None,
      day: Optional[str] = None,
      matched_filter: Optional[str] = None,
  ) -> dict:
    where_clauses = []
    params: list[object] = []
    if person_id:
      where_clauses.append("person_id = ?")
      params.append(person_id)
    if day:
      where_clauses.append("date(created_at) = ?")
      params.append(day)
    if matched_filter == "match":
      where_clauses.append("matched = 1")
    elif matched_filter == "no-match":
      where_clauses.append("matched = 0")

    where_sql = ""
    if where_clauses:
      where_sql = "WHERE " + " AND ".join(where_clauses)

    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      latest_event = connection.execute(
          """
          SELECT id, person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at
          FROM ring_events
          {where_sql}
          ORDER BY id DESC
          LIMIT 1
          """.format(where_sql=where_sql),
          params,
      ).fetchone()

      recent_events = connection.execute(
          """
          SELECT id, person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at
          FROM ring_events
          {where_sql}
          ORDER BY id DESC
          LIMIT 24
          """.format(where_sql=where_sql),
          params,
      ).fetchall()

      latest_captures = []
      if latest_event is not None:
        latest_captures = connection.execute(
            """
            SELECT event_id, sequence_index, matched, similarity, threshold, detected_faces, error, image_path, created_at
            FROM ring_captures
            WHERE event_id = ?
            ORDER BY sequence_index
            """,
            (latest_event["id"],),
        ).fetchall()

    return {
        "system_status": f"Letztes Update: {self._local_time_label()}",
        "available_people": self.list_person_ids(),
        "latest_event": self._event_to_dict(latest_event) if latest_event else None,
        "latest_captures": [self._capture_to_dict(row) for row in latest_captures],
        "recent_events": [self._event_to_dict(row) for row in recent_events],
    }

  def fetch_live_snapshot(self) -> bytes:
    with urlrequest.urlopen(DEFAULT_ESP_SNAPSHOT_URL, timeout=8) as response:
      return response.read()

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

  def _capture_to_dict(self, row: sqlite3.Row) -> dict:
    return {
        "event_id": row["event_id"],
        "sequence_index": row["sequence_index"],
        "matched": bool(row["matched"]),
        "similarity": row["similarity"],
        "threshold": row["threshold"],
        "detected_faces": row["detected_faces"],
        "error": row["error"],
        "image_url": f"/captures/{row['image_path']}",
        "created_at": row["created_at"],
    }

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
  def dashboard():
    response = make_response(DEFAULT_DASHBOARD_HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

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

  @app.get("/api/dashboard")
  def dashboard_data():
    return jsonify(
        service.dashboard_data(
            person_id=request.args.get("person_id"),
            day=request.args.get("day"),
            matched_filter=request.args.get("matched"),
        )
    )

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

  @app.post("/api/ring-capture")
  def ring_capture():
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
        or DEFAULT_PERSON_ID
    )
    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild uebergeben."}), 400

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
