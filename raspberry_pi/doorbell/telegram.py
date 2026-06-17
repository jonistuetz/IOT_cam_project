"""Telegram-Anbindung: Benachrichtigung und Freigabe von Klingelereignissen.

``TelegramNotifier`` hält eine Referenz auf den ``FaceVerifierService`` und
nutzt dessen Datenbank-, Netzwerk- und Event-Helfer. Token und Chat-ID werden
bei jedem Zugriff frisch vom Service gelesen, damit Config-Änderungen sofort
greifen.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import requests

from .constants import UNKNOWN_PERSON_ID
from .database import get_app_state, set_app_state
from .log import debug
from .utils.timeutils import utc_now


TELEGRAM_SEND_PHOTO_TIMEOUT_S = 15
TELEGRAM_SEND_MESSAGE_TIMEOUT_S = 5
TELEGRAM_GET_UPDATES_TIMEOUT_S = 5
TELEGRAM_ANSWER_CALLBACK_TIMEOUT_S = 5


class TelegramNotifier:
  def __init__(self, service) -> None:
    self._service = service

  @property
  def _token(self) -> str:
    return self._service.telegram_bot_token

  @property
  def _chat_id(self) -> str:
    return self._service.telegram_chat_id

  @property
  def enabled(self) -> bool:
    return bool(self._token and self._chat_id)

  def notify_event(self, event_id: int) -> None:
    if not self.enabled:
      return

    network_status = self._service.network_status()
    if not network_status["internet_ok"]:
      debug(
          f"telegram skipped event_id={event_id}: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    with sqlite3.connect(self._service.db_path) as connection:
      connection.row_factory = sqlite3.Row
      event_row = connection.execute(
          """
          SELECT id, person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at
          FROM ring_events
          WHERE id = ?
          """,
          (event_id,),
      ).fetchone()
      if event_row is None:
        return

      action_row = connection.execute(
          """
          SELECT telegram_notified_at
          FROM event_actions
          WHERE event_id = ?
          """,
          (event_id,),
      ).fetchone()
      if action_row is not None and action_row["telegram_notified_at"]:
        return

      capture_row = connection.execute(
          """
          SELECT image_path, matched, similarity, detected_faces, error, sequence_index
          FROM ring_captures
          WHERE event_id = ?
          ORDER BY matched DESC, similarity DESC, sequence_index ASC
          LIMIT 1
          """,
          (event_id,),
      ).fetchone()

    event = self._service._event_to_dict(event_row)
    caption = self._build_caption(event, capture_row)
    image_path = self._service.captures_dir / capture_row["image_path"] if capture_row is not None else None

    try:
      # Externe Kommunikation: ab hier spricht der Raspberry Pi per HTTPS mit
      # der Telegram Bot API. Der ESP sieht davon nichts; er fragt später nur
      # den lokalen Pi-Endpunkt /api/ring-decision ab.
      message_id = self._send_photo(image_path, caption, event_id)
    except Exception as exc:
      debug(f"telegram send failed event_id={event_id}: {exc}")
      return

    now = utc_now()
    with sqlite3.connect(self._service.db_path) as connection:
      connection.execute(
          """
          INSERT INTO event_actions (event_id, telegram_message_id, telegram_notified_at)
          VALUES (?, ?, ?)
          ON CONFLICT(event_id) DO UPDATE SET
            telegram_message_id = excluded.telegram_message_id,
            telegram_notified_at = excluded.telegram_notified_at
          """,
          (event_id, message_id, now),
      )
      connection.commit()

  def _build_caption(self, event: dict, capture_row: Optional[sqlite3.Row]) -> str:
    person_id = event["person_id"] or UNKNOWN_PERSON_ID
    recommendation = "Zulassen" if event["matched"] else "Ablehnen"
    lines = [
        f"Klingelereignis #{event['id']}",
        f"Person: {person_id}",
        f"Empfehlung: {recommendation}",
        f"Matches: {event['matched_images']}/{event['total_images']}",
        f"Zeit: {event['created_at_local']}",
    ]
    if event["best_similarity"] is not None:
      lines.append(f"Beste Confidence: {event['best_similarity']:.2f}")
    if capture_row is not None:
      if capture_row["detected_faces"] is not None:
        lines.append(f"Gesichter: {capture_row['detected_faces']}")
      if capture_row["error"]:
        lines.append(f"Hinweis: {capture_row['error']}")
    return "\n".join(lines)

  def _api_url(self, method: str) -> str:
    # Telegram nutzt klassische HTTPS-Endpunkte pro Bot-Methode, z. B.
    # sendPhoto, sendMessage, getUpdates und answerCallbackQuery.
    return f"https://api.telegram.org/bot{self._token}/{method}"

  def _reply_markup(self, event_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [[
                {"text": "Reinlassen", "callback_data": f"doorbell:approve:{event_id}"},
                {"text": "Ablehnen", "callback_data": f"doorbell:deny:{event_id}"},
            ]]
        }
    )

  def _send_photo(self, image_path: Optional[Path], caption: str, event_id: int) -> int:
    if image_path is not None and image_path.exists():
      with image_path.open("rb") as image_file:
        # sendPhoto ist ein multipart/form-data POST: Textfelder liegen in
        # data=, das JPEG im files=-Teil. Das ist die von requests angebotene
        # Standardabbildung für Datei-Uploads über HTTP.
        response = requests.post(
            self._api_url("sendPhoto"),
            data={
                "chat_id": self._chat_id,
                "caption": caption,
                "reply_markup": self._reply_markup(event_id),
            },
            files={"photo": image_file},
            timeout=TELEGRAM_SEND_PHOTO_TIMEOUT_S,
        )
    else:
      # Fallback ohne Bild: weiterhin POST, aber nur Formularfelder.
      response = requests.post(
          self._api_url("sendMessage"),
          data={
              "chat_id": self._chat_id,
              "text": caption,
              "reply_markup": self._reply_markup(event_id),
          },
          timeout=TELEGRAM_SEND_MESSAGE_TIMEOUT_S,
      )

    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
      raise RuntimeError(f"telegram api error: {payload}")
    return int(payload["result"]["message_id"])

  def send_test_message(self) -> dict:
    if not self.enabled:
      return {"ok": False, "error": "Telegram ist nicht vollstaendig konfiguriert."}

    network_status = self._service.network_status()
    if not network_status["internet_ok"]:
      return {
          "ok": False,
          "error": "Kein Internet/Uplink für Telegram-Test.",
          "network": network_status,
      }

    try:
      response = requests.post(
          self._api_url("sendMessage"),
          data={
              "chat_id": self._chat_id,
              "text": "Smart Doorbell Telegram-Test: Verbindung funktioniert.",
          },
          timeout=TELEGRAM_SEND_MESSAGE_TIMEOUT_S,
      )
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      return {"ok": False, "error": str(exc), "network": network_status}

    if not payload.get("ok"):
      return {"ok": False, "error": f"Telegram API meldet Fehler: {payload}", "network": network_status}

    return {"ok": True, "message": "Telegram-Testnachricht wurde gesendet.", "network": network_status}

  def sync_updates(self) -> None:
    if not self.enabled:
      return

    network_status = self._service.network_status()
    if not network_status["internet_ok"]:
      debug(
          "telegram update sync skipped: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    offset = get_app_state(self._service.db_path, "telegram_update_offset")
    # Kurzes Polling statt Webhook: Der Pi muss von außen nicht erreichbar sein.
    # offset verhindert, dass bereits verarbeitete Callback-Updates erneut
    # ausgewertet werden. timeout=0 bedeutet: kein Telegram-Long-Polling.
    params = {"timeout": 0, "allowed_updates": json.dumps(["callback_query"])}
    if offset is not None:
      params["offset"] = str(int(offset))

    try:
      response = requests.get(
          self._api_url("getUpdates"),
          params=params,
          timeout=TELEGRAM_GET_UPDATES_TIMEOUT_S,
      )
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      debug(f"telegram update sync failed: {exc}")
      return

    if not payload.get("ok"):
      debug(f"telegram update sync returned error payload: {payload}")
      return

    next_offset = None
    for update in payload.get("result", []):
      next_offset = int(update["update_id"]) + 1
      callback_query = update.get("callback_query")
      if callback_query is not None:
        self._process_callback(callback_query)

    if next_offset is not None:
      set_app_state(self._service.db_path, "telegram_update_offset", str(next_offset))

  def _process_callback(self, callback_query: dict) -> None:
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}

    if str(chat.get("id", "")) != self._chat_id:
      self._answer_callback(callback_id, "Dieser Chat ist nicht freigeschaltet.")
      return

    # callback_data ist ein kleines eigenes Protokoll:
    # doorbell:<approve|deny>:<event_id>. Dadurch kann der Pi die Telegram-
    # Button-Antwort wieder eindeutig einem lokalen Klingelereignis zuordnen.
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "doorbell" or parts[1] not in {"approve", "deny"}:
      self._answer_callback(callback_id, "Unbekannter Befehl.")
      return

    try:
      event_id = int(parts[2])
    except ValueError:
      self._answer_callback(callback_id, "Ungültige Ereignis-ID.")
      return

    decision = parts[1]
    now = utc_now()

    with sqlite3.connect(self._service.db_path) as connection:
      connection.row_factory = sqlite3.Row
      existing = connection.execute(
          "SELECT decision FROM event_actions WHERE event_id = ?",
          (event_id,),
      ).fetchone()
      if existing is not None and existing["decision"]:
        self._answer_callback(callback_id, "Dieses Klingeln wurde schon entschieden.")
        return

      connection.execute(
          """
          INSERT INTO event_actions (event_id, decision, decision_source, decided_at)
          VALUES (?, ?, ?, ?)
          ON CONFLICT(event_id) DO UPDATE SET
            decision = excluded.decision,
            decision_source = excluded.decision_source,
            decided_at = excluded.decided_at
          """,
          (event_id, decision, "telegram", now),
      )
      connection.commit()

    self._answer_callback(
        callback_id,
        "Zutritt freigegeben." if decision == "approve" else "Zutritt abgelehnt.",
    )

  def _answer_callback(self, callback_id: Optional[str], text: str) -> None:
    if not callback_id or not self.enabled:
      return
    try:
      response = requests.post(
          self._api_url("answerCallbackQuery"),
          data={"callback_query_id": callback_id, "text": text},
          timeout=TELEGRAM_ANSWER_CALLBACK_TIMEOUT_S,
      )
      response.raise_for_status()
    except Exception as exc:
      debug(f"telegram callback answer failed: {exc}")
