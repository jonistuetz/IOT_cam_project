"""Registrierung aller HTTP-Routen auf einer Flask-App.

Die Handler sind dünn: sie lesen die Anfrage aus und delegieren an den
``FaceVerifierService``. Aufgerufen aus ``doorbell.web.app.create_app``.
"""

from typing import Optional
from urllib import error as urlerror

from flask import jsonify, make_response, redirect, render_template, request, send_from_directory


def register_routes(app, service) -> None:
  @app.get("/")
  def index():
    return redirect("/setup")

  @app.get("/setup")
  def setup_page():
    response = make_response(render_template("setup.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

  @app.get("/health")
  def health():
    network_status = service.network_status()
    return jsonify(
        {
            "status": "ok" if network_status["internet_ok"] else "degraded",
            "model": service.model_name,
            "threshold": service.similarity_threshold,
            "db_path": str(service.db_path),
            "telegram_enabled": service.telegram_enabled,
            "setup_complete": service.setup_complete,
            "config": service.config_status(),
            "network": network_status,
        }
    )

  @app.get("/api/network-status")
  def network_status():
    status = service.network_status()
    return jsonify({"ok": status["internet_ok"], **status}), (200 if status["internet_ok"] else 503)

  @app.get("/api/persons")
  def list_people():
    return jsonify({"people": service.list_people()})

  @app.post("/api/persons/<path:person_id>/active")
  def set_person_active(person_id: str):
    payload = request.get_json(silent=True) or request.form
    active_value = payload.get("active")
    if isinstance(active_value, bool):
      active = active_value
    else:
      active = str(active_value).lower() in {"1", "true", "yes", "on", "active"}

    try:
      result = service.set_person_active(person_id=person_id, active=active)
      return jsonify({"ok": True, **result})
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.delete("/api/persons/<path:person_id>")
  def delete_person(person_id: str):
    result = service.delete_person(person_id=person_id)
    if result["deleted_references"] == 0:
      return jsonify({"ok": False, "error": "Person oder Referenzbilder nicht gefunden.", **result}), 404
    return jsonify({"ok": True, **result})

  @app.post("/api/enroll-session/discard")
  def discard_enroll_session():
    payload = request.get_json(silent=True) or request.form
    try:
      result = service.discard_enroll_session(
          person_id=str(payload.get("person_id") or ""),
          session_id=str(payload.get("session_id") or ""),
      )
      return jsonify({"ok": True, **result})
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.get("/api/config")
  def config_status():
    return jsonify(service.config_status())

  @app.post("/api/config")
  def save_config():
    payload = request.get_json(silent=True) or request.form
    telegram_bot_token = payload.get("telegram_bot_token")
    telegram_chat_id = payload.get("telegram_chat_id")
    esp_snapshot_url = payload.get("esp_snapshot_url")
    similarity_threshold = payload.get("similarity_threshold")

    try:
      status = service.update_config(
          telegram_bot_token=telegram_bot_token,
          telegram_chat_id=telegram_chat_id,
          esp_snapshot_url=esp_snapshot_url,
          similarity_threshold=similarity_threshold,
      )
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, **status})

  @app.post("/api/test-telegram")
  def test_telegram():
    payload = service.send_telegram_test_message()
    return jsonify(payload), (200 if payload.get("ok") else 400)

  @app.post("/api/system/shutdown")
  def system_shutdown():
    payload = service.request_safe_shutdown()
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.get("/api/wifi/status")
  def wifi_status():
    payload = service.wifi_setup("status")
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.post("/api/wifi/scan")
  def wifi_scan():
    payload = service.wifi_setup("scan")
    return jsonify(payload), (200 if payload.get("ok") else 500)

  @app.post("/api/wifi/connect")
  def wifi_connect():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup(
        "connect",
        ssid=payload.get("ssid"),
        password=payload.get("password"),
        name=payload.get("name"),
    )
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.post("/api/wifi/activate")
  def wifi_activate():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup("activate", name=payload.get("name"))
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.post("/api/wifi/priority")
  def wifi_priority():
    payload = request.get_json(silent=True) or request.form
    result = service.wifi_setup("priority", name=payload.get("name"), value=payload.get("priority"))
    return jsonify(result), (200 if result.get("ok") else 400)

  @app.get("/api/live-snapshot")
  def live_snapshot():
    # Browser -> Pi -> ESP: Die Setup-Seite ruft den Pi auf; der Pi holt das
    # JPEG per HTTP GET vom ESP und reicht es mit no-store weiter.
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
    service._debug("enroll request received")
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    note = request.form.get("note")
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    filename = request.files["image"].filename if "image" in request.files else "raw-request-body"
    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

    service._debug(f"enroll start person_id={person_id} filename={filename} bytes={len(image_bytes)}")
    try:
      payload = service.enroll_embedding(person_id=person_id, image_bytes=image_bytes, note=note)
      service._debug(f"enroll ok person_id={person_id} filename={filename} reference_count={payload['reference_count']}")
      return jsonify({"ok": True, **payload}), 201
    except Exception as exc:
      service._debug(f"enroll failed person_id={person_id} filename={filename}: {exc}")
      return jsonify({"ok": False, "error": str(exc)}), 400

  @app.post("/api/enroll-batch")
  def enroll_batch():
    service._debug("enroll_batch request received")
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    note = request.form.get("note")
    if not person_id:
      return jsonify({"ok": False, "error": "person_id fehlt."}), 400

    uploaded_files = request.files.getlist("images") or request.files.getlist("image")
    if not uploaded_files:
      return jsonify({"ok": False, "error": "Es wurden keine Bilder übergeben."}), 400

    service._debug(f"enroll_batch start person_id={person_id} images={len(uploaded_files)}")
    results = []
    success_count = 0
    for index, uploaded_file in enumerate(uploaded_files, start=1):
      filename = uploaded_file.filename or f"image_{index}"
      image_bytes = uploaded_file.read()
      service._debug(f"enroll_batch image {index}/{len(uploaded_files)} filename={filename} bytes={len(image_bytes)}")
      if not image_bytes:
        results.append({"filename": filename, "ok": False, "error": "Datei ist leer."})
        continue

      try:
        payload = service.enroll_embedding(
            person_id=person_id,
            image_bytes=image_bytes,
            note=note or f"setup-upload:{filename}",
        )
        success_count += 1
        service._debug(f"enroll_batch image {index}/{len(uploaded_files)} ok reference_count={payload['reference_count']}")
        results.append({"filename": filename, "ok": True, **payload})
      except Exception as exc:
        service._debug(f"enroll_batch image {index}/{len(uploaded_files)} failed: {exc}")
        results.append({"filename": filename, "ok": False, "error": str(exc)})

    service._debug(
        f"enroll_batch done person_id={person_id} successful={success_count}/{len(uploaded_files)}"
    )
    return jsonify(
        {
            "ok": success_count > 0,
            "person_id": person_id,
            "received_images": len(uploaded_files),
            "successful_images": success_count,
            "failed_images": len(uploaded_files) - success_count,
            "results": results,
        }
    ), (201 if success_count > 0 else 400)

  @app.post("/api/enroll-from-esp")
  def enroll_from_esp():
    person_id = request.form.get("person_id") or request.headers.get("X-Person-Id")
    count_value = request.form.get("count") or request.args.get("count") or "1"
    step = request.form.get("step") or request.args.get("step")
    instruction = request.form.get("instruction") or request.args.get("instruction")
    session_id = request.form.get("session_id") or request.args.get("session_id")
    note_parts = ["esp-camera"]
    if session_id:
      note_parts.append(f"session:{session_id}")
    if step:
      note_parts.append(f"step:{step}")
    if instruction:
      note_parts.append(instruction)
    try:
      count = int(count_value)
      payload = service.enroll_from_esp(
          person_id=person_id or "",
          count=count,
          note=" | ".join(note_parts),
          session_id=session_id,
      )
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(payload), (201 if payload["ok"] else 400)

  @app.post("/api/verify")
  def verify():
    # Einzelbild-Verifikation: akzeptiert sowohl multipart/form-data als auch
    # rohe JPEG-Bytes im Body. Der ESP nutzt den Raw-Body-Weg.
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
    )

    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

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
    # Klingel-Burst: Jeder POST enthält genau ein JPEG. sequence/total/event_id
    # verbinden die Einzelbilder serverseitig zu einem gemeinsamen Ereignis.
    person_id = (
        request.form.get("person_id")
        or request.headers.get("X-Person-Id")
        or request.args.get("person_id")
    )
    image_bytes = _read_image_bytes()
    if image_bytes is None:
      return jsonify({"ok": False, "error": "Es wurde kein Bild übergeben."}), 400

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

  @app.post("/api/esp-log")
  def esp_log():
    # Remote-Logs des ESP bleiben absichtlich simpel: text/plain im Body,
    # MAC-Adresse im Header. Persistiert wird hier nichts, Ausgabe landet im
    # systemd/journalctl-Log des Pi-Dienstes.
    mac = request.headers.get("X-ESP-MAC", "unknown")
    message = request.get_data(as_text=True).strip()
    if message:
      print(f"[ESP {mac}] {message}", flush=True)
    return jsonify({"ok": True})

  @app.get("/api/ring-decision")
  def ring_decision():
    # Polling-Endpunkt für den ESP. GET ist ausreichend, weil nur der aktuelle
    # Entscheidungsstand gelesen wird; Änderungen kommen über Telegram-Callbacks.
    event_id_value = request.args.get("event_id")
    if not event_id_value:
      return jsonify({"ok": False, "error": "event_id fehlt."}), 400

    try:
      event_id = int(event_id_value)
    except ValueError:
      return jsonify({"ok": False, "error": "event_id ist ungültig."}), 400

    return jsonify(service.get_event_decision(event_id))

  def _read_image_bytes() -> Optional[bytes]:
    if "image" in request.files:
      return request.files["image"].read()

    if request.data:
      return request.data

    return None
