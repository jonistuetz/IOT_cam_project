"""SQLite-Persistenzschicht: Schema-Anlage, Migrationen und app_state.

Die Funktionen sind bewusst zustandslos und bekommen den Datenbankpfad jeweils
übergeben. Die fachlichen Abfragen (Enrollment, Verification, Events) liegen
weiterhin in den jeweiligen Mixins; hier steht nur das Gemeinsame.
"""

import sqlite3
from pathlib import Path
from typing import Optional


def ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
  columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
  existing_names = {column[1] for column in columns}
  if column_name not in existing_names:
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def init_schema(db_path: Path) -> None:
  with sqlite3.connect(db_path) as connection:
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
    ensure_column(
        connection,
        table_name="ring_events",
        column_name="matched_images",
        column_definition="INTEGER NOT NULL DEFAULT 0",
    )
    connection.commit()


def get_app_state(db_path: Path, key: str) -> Optional[str]:
  with sqlite3.connect(db_path) as connection:
    row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
  return None if row is None else str(row[0])


def set_app_state(db_path: Path, key: str, value: str) -> None:
  with sqlite3.connect(db_path) as connection:
    connection.execute(
        """
        INSERT INTO app_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    connection.commit()
