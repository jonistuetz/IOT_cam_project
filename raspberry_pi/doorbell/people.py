"""Personenverwaltung: auflisten, aktivieren/deaktivieren, löschen.

Als Mixin gestaltet; arbeitet direkt auf den Tabellen ``reference_embeddings``
und ``person_settings`` über den Datenbankpfad des Dienstes.
"""

import sqlite3


class PeopleMixin:
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

  def _person_is_active(self, person_id: str) -> bool:
    with sqlite3.connect(self.db_path) as connection:
      row = connection.execute(
          "SELECT active FROM person_settings WHERE person_id = ?",
          (person_id,),
      ).fetchone()
    return row is None or bool(row[0])
