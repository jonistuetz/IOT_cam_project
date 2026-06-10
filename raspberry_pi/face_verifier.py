import argparse
import json
import os
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, make_response, redirect, request, send_from_directory
from insightface.app import FaceAnalysis


DEFAULT_DB_PATH = Path(__file__).with_name("face_verification.db")
DEFAULT_CAPTURES_DIR = Path(__file__).with_name("captures")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_MODEL_NAME = "buffalo_sc"
DEFAULT_SIMILARITY_THRESHOLD = 0.60
DEFAULT_DET_SIZE = (640, 640)
UNKNOWN_PERSON_ID = "unknown"
DEFAULT_ESP_SNAPSHOT_URL = "http://10.42.0.172/snapshot"
DEFAULT_REQUIRED_MATCHES_FOR_ACCESS = 2
SAFE_POWEROFF_HELPER = "/usr/local/sbin/hs-iot-safe-poweroff"
WIFI_SETUP_HELPER = "/usr/local/sbin/hs-iot-wifi-setup"

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

DEFAULT_SETUP_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Smart Doorbell Setup</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #edf2ee;
      --card: #fffdf7;
      --text: #1f2933;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --ok: #166534;
      --bad: #991b1b;
      --muted: #637083;
      --line: rgba(31, 41, 51, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Helvetica, Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.16), transparent 34rem),
        linear-gradient(135deg, #f6f0df 0%, #e9f2ee 52%, #dce8e3 100%);
      color: var(--text);
      padding: 24px;
    }
    main {
      width: min(980px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }
    .hero, .card {
      background: rgba(255, 253, 247, 0.92);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 22px 55px rgba(31, 41, 51, 0.12);
      padding: 22px;
    }
    h1, h2 {
      margin: 0 0 10px;
      letter-spacing: -0.03em;
    }
    p {
      color: var(--muted);
      line-height: 1.55;
      margin: 0 0 12px;
    }
    label {
      display: grid;
      gap: 6px;
      margin: 12px 0;
      font-weight: 700;
    }
    input, button {
      border-radius: 14px;
      border: 1px solid var(--line);
      font: inherit;
      padding: 11px 13px;
    }
    form button {
      margin: 8px 8px 0 0;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 12px 0;
    }
    input[type="file"] {
      background: #fff;
    }
    button {
      background: var(--accent);
      color: white;
      border: 0;
      cursor: pointer;
      font-weight: 700;
    }
    button.secondary {
      background: #334155;
    }
    button:hover {
      background: var(--accent-dark);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 18px;
    }
    .method-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 14px 0;
    }
    .method-tabs button {
      background: #dbe8e4;
      color: var(--text);
    }
    .method-tabs button.active {
      background: var(--accent);
      color: white;
    }
    .method-panel[hidden] {
      display: none;
    }
    .guide {
      border-left: 4px solid var(--accent);
      background: rgba(15, 118, 110, 0.08);
      border-radius: 14px;
      padding: 12px;
      margin: 12px 0;
      color: var(--text);
      line-height: 1.55;
    }
    .guide strong {
      font-family: Helvetica, Arial, sans-serif;
      font-weight: 800;
    }
    .status {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }
    .pill {
      border-radius: 999px;
      padding: 7px 11px;
      background: rgba(99, 112, 131, 0.12);
      color: var(--muted);
      font-weight: 700;
    }
    .pill.ok { background: rgba(22, 101, 52, 0.12); color: var(--ok); }
    .pill.bad { background: rgba(153, 27, 27, 0.12); color: var(--bad); }
    .result {
      white-space: pre-wrap;
      background: rgba(15, 118, 110, 0.08);
      border-radius: 16px;
      padding: 12px;
      min-height: 44px;
      color: var(--text);
    }
    .people {
      display: grid;
      gap: 10px;
    }
    .person {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: rgba(255, 255, 255, 0.55);
    }
    .person-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .person-actions button {
      padding: 8px 10px;
      font-size: 14px;
    }
    .wifi-list {
      display: grid;
      gap: 10px;
      margin: 12px 0;
    }
    .wifi-item {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.55);
      padding: 10px;
    }
    .wifi-item button {
      margin-top: 8px;
    }
    .wifi-summary {
      display: grid;
      gap: 6px;
    }
    .preview {
      width: 100%;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: #111;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      margin-top: 10px;
    }
    .danger {
      background: #991b1b;
    }
    .danger:hover {
      background: #7f1d1d;
    }
    @media (max-width: 680px) {
      body { padding: 14px; }
      .hero, .card { padding: 18px; border-radius: 20px; }
    }
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>Smart Doorbell Setup</h1>
      <p>Lokale Admin-Seite für Telegram-Konfiguration, Systemstatus und Referenzbilder. Bestehende Gesichter bleiben erhalten.</p>
      <div id="status" class="status"></div>
    </section>

    <section class="grid">
      <article class="card">
        <h2>Telegram</h2>
        <p>Token wird lokal auf dem Raspberry Pi gespeichert und in der Oberfläche nur maskiert angezeigt.</p>
        <form id="configForm">
          <label>Bot Token
            <input id="telegramBotToken" name="telegram_bot_token" autocomplete="off" placeholder="123456:ABCDEF...">
          </label>
          <label>Chat ID
            <input id="telegramChatId" name="telegram_chat_id" autocomplete="off" placeholder="123456789">
          </label>
          <button type="submit">Telegram speichern</button>
          <button class="secondary" id="testTelegramButton" type="button">Testnachricht senden</button>
        </form>
        <div id="configResult" class="result">Noch keine Aktion ausgeführt.</div>
      </article>

      <article class="card">
        <h2>Zulassungsschwelle</h2>
        <p>Legt fest, ab welcher Face-Similarity ein einzelnes Bild als Match gilt. Höher = strenger, niedriger = toleranter. Standardwert ist 0.60.</p>
        <form id="thresholdForm">
          <label>Similarity Threshold
            <input id="similarityThreshold" name="similarity_threshold" type="number" min="0" max="1" step="0.01" placeholder="0.60">
          </label>
          <button type="submit">Schwelle speichern</button>
        </form>
        <div id="thresholdResult" class="result">Standardwert 0.60 ist aktiv und in der Config hinterlegt. Nur bei Bedarf anpassen.</div>
      </article>

      <article class="card">
        <h2>System</h2>
        <p>Fährt den Raspberry Pi sauber herunter. Danach erst die Stromversorgung trennen, wenn der Pi vollständig aus ist.</p>
        <button class="danger" id="shutdownButton" type="button">Raspberry Pi sicher herunterfahren</button>
        <div id="systemResult" class="result">Noch keine Systemaktion ausgeführt.</div>
      </article>

      <article class="card">
        <h2>Internet-WLAN</h2>
        <p>Verwaltet nur den USB-WLAN-Adapter <strong>wlan1</strong> für Internetzugang. Der lokale Hotspot auf <strong>wlan0</strong> bleibt unverändert.</p>
        <div class="toolbar">
          <button id="wifiRefreshButton" type="button">Status aktualisieren</button>
          <button class="secondary" id="wifiScanButton" type="button">WLANs suchen</button>
        </div>
        <div id="wifiStatus" class="result">Noch kein WLAN-Status geladen.</div>
        <h3>Gespeicherte WLAN-Profile</h3>
        <div id="wifiProfiles" class="wifi-list">Noch keine Profile geladen.</div>
        <h3>Neues WLAN verbinden</h3>
        <form id="wifiConnectForm">
          <label>SSID
            <input id="wifiSsid" name="ssid" required placeholder="Mein WLAN">
          </label>
          <label>Passwort
            <input id="wifiPassword" name="password" type="password" autocomplete="new-password">
          </label>
          <label>Profilname optional
            <input id="wifiProfileName" name="name" placeholder="home-wifi">
          </label>
          <button type="submit">Mit WLAN verbinden</button>
        </form>
        <h3>Gefundene WLANs</h3>
        <div id="wifiNetworks" class="wifi-list">Noch kein Scan ausgeführt.</div>
        <div id="wifiResult" class="result">Noch keine WLAN-Aktion ausgeführt.</div>
      </article>

      <article class="card">
        <h2>Gesicht anlernen</h2>
        <p>Wähle, ob du vorhandene Bilder hochladen oder direkt mit der ESP32-CAM realistische Referenzbilder aufnehmen möchtest.</p>
        <div class="method-tabs">
          <button id="showEspEnroll" class="active" type="button">ESP-Kamera</button>
          <button id="showUploadEnroll" type="button">Manueller Upload</button>
        </div>

        <div id="espEnrollPanel" class="method-panel">
          <h3>Geführte ESP-Aufnahme</h3>
          <p>Es werden fest 12 Bilder aufgenommen: bei ca. 0,5 m und ca. 1 m Abstand jeweils 2 frontal, 2 leicht links und 2 leicht rechts. Du klickst jedes Bild einzeln weiter.</p>
          <p>Vor dem Start muss die Klingel aus dem Deep Sleep geweckt werden, z. B. durch Tastendruck oder Bewegungssensor. Warte danach, bis der ESP mit dem WLAN verbunden ist: Wenn die grüne LED dauerhaft leuchtet, ist er bereit. Nimm am besten zuerst eine Vorschau auf und starte danach die geführte Aufnahme.</p>
          <div class="guide" id="espGuide">
            Stelle dich vor die ESP32-CAM. Starte erst, wenn dein Gesicht gut sichtbar ist.
          </div>
          <form id="espEnrollForm">
            <label>Person ID
              <input id="espPersonId" name="person_id" required placeholder="jonathan">
            </label>
            <button type="submit">Geführte Aufnahme starten</button>
            <button class="danger" id="espDiscardSessionButton" type="button" hidden>Session verwerfen</button>
            <button class="secondary" id="espPreviewButton" type="button">Vorschau aufnehmen</button>
          </form>
          <img id="espPreview" class="preview" alt="ESP-Kamera-Vorschau">
          <div id="espEnrollResult" class="result">Noch keine ESP-Aufnahme gestartet.</div>
        </div>

        <div id="uploadEnrollPanel" class="method-panel" hidden>
          <h3>Manueller Upload</h3>
          <p>Lade mehrere Bilder derselben Person hoch. Pro gültigem Bild wird ein Embedding in der bestehenden Datenbank gespeichert.</p>
          <form id="enrollForm">
            <label>Person ID
              <input id="personId" name="person_id" required placeholder="jonathan">
            </label>
            <label>Bilder
              <input id="images" name="images" type="file" accept="image/*" multiple required>
            </label>
            <button type="submit">Bilder anlernen</button>
          </form>
          <div id="enrollResult" class="result">Noch keine Bilder hochgeladen.</div>
        </div>
      </article>
    </section>

    <section class="card">
      <h2>Gespeicherte Personen</h2>
      <div id="people" class="people">Lade Personen...</div>
    </section>
  </main>

  <script>
    function escapeHtml(value) {
      return String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
    }

    function pill(label, ok) {
      return `<span class="pill ${ok ? "ok" : "bad"}">${escapeHtml(label)}</span>`;
    }

    function highlightInstruction(value) {
      let html = escapeHtml(value);
      ["0,5 m", "1 m", "frontal", "links", "rechts"].forEach((term) => {
        html = html.replaceAll(term, `<strong>${term}</strong>`);
      });
      return html;
    }

    const espCaptureGuide = [
      "Bild 1/12: Abstand ca. 0,5 m, frontal in die Kamera schauen.",
      "Bild 2/12: Abstand ca. 0,5 m, frontal bleiben, kleine Variation.",
      "Bild 3/12: Abstand ca. 0,5 m, Kopf leicht nach links drehen.",
      "Bild 4/12: Abstand ca. 0,5 m, weiter leicht links, kleine Variation.",
      "Bild 5/12: Abstand ca. 0,5 m, Kopf leicht nach rechts drehen.",
      "Bild 6/12: Abstand ca. 0,5 m, weiter leicht rechts, kleine Variation.",
      "Bild 7/12: Abstand ca. 1 m, frontal in die Kamera schauen.",
      "Bild 8/12: Abstand ca. 1 m, frontal bleiben, kleine Variation.",
      "Bild 9/12: Abstand ca. 1 m, Kopf leicht nach links drehen.",
      "Bild 10/12: Abstand ca. 1 m, weiter leicht links, kleine Variation.",
      "Bild 11/12: Abstand ca. 1 m, Kopf leicht nach rechts drehen.",
      "Bild 12/12: Abstand ca. 1 m, weiter leicht rechts, kleine Variation.",
    ];
    let espEnrollState = {
      active: false,
      personId: "",
      sessionId: "",
      stepIndex: 0,
      results: [],
    };

    function showEnrollmentMethod(method) {
      const espActive = method === "esp";
      document.getElementById("espEnrollPanel").hidden = !espActive;
      document.getElementById("uploadEnrollPanel").hidden = espActive;
      document.getElementById("showEspEnroll").classList.toggle("active", espActive);
      document.getElementById("showUploadEnroll").classList.toggle("active", !espActive);
    }

    function setEspGuide(value) {
      document.getElementById("espGuide").innerHTML = highlightInstruction(value);
    }

    async function loadStatus() {
      const [configResponse, healthResponse, peopleResponse] = await Promise.all([
        fetch("/api/config"),
        fetch("/health"),
        fetch("/api/persons"),
      ]);
      const config = await configResponse.json();
      const health = await healthResponse.json();
      const people = await peopleResponse.json();
      const personList = people.people || [];
      const activePeople = personList.filter((person) => person.active).length;

      document.getElementById("status").innerHTML = [
        pill("Telegram " + (config.telegram_enabled ? "aktiv" : "fehlt"), config.telegram_enabled),
        pill("Internet " + (health.network && health.network.internet_ok ? "ok" : "nicht ok"), health.network && health.network.internet_ok),
        pill("Aktive Personen: " + activePeople, activePeople > 0),
      ].join("");

      document.getElementById("telegramChatId").value = config.telegram_chat_id || "";
      document.getElementById("telegramBotToken").placeholder = config.telegram_bot_token_masked || "123456:ABCDEF...";
      const activeThreshold = config.similarity_threshold == null ? "0.60" : Number(config.similarity_threshold).toFixed(2);
      document.getElementById("similarityThreshold").value = activeThreshold;
      setResult("thresholdResult", "Aktive Schwelle: " + activeThreshold + " (Standard 0.60, in der Config hinterlegt). Nur bei Bedarf anpassen.");

      renderPeople(personList);
    }

    function setResult(id, data) {
      document.getElementById(id).textContent =
          typeof data === "string" ? data : JSON.stringify(data, null, 2);
    }

    function renderPeople(personList) {
      const peopleNode = document.getElementById("people");
      peopleNode.innerHTML = "";
      if (personList.length === 0) {
        peopleNode.textContent = "Noch keine Personen gespeichert.";
        return;
      }

      personList.forEach((person) => {
        const card = document.createElement("div");
        card.className = "person";

        const title = document.createElement("strong");
        title.textContent = person.person_id;
        card.appendChild(title);
        card.appendChild(document.createElement("br"));

        card.appendChild(document.createTextNode("Status: "));
        const status = document.createElement("span");
        status.className = "pill " + (person.active ? "ok" : "bad");
        status.textContent = person.active ? "aktiv" : "deaktiviert";
        card.appendChild(status);
        card.appendChild(document.createElement("br"));

        card.appendChild(document.createTextNode("Referenzbilder: " + person.reference_count));
        card.appendChild(document.createElement("br"));
        card.appendChild(document.createTextNode("Aktualisiert: " + (person.updated_at || "n/a")));

        const actions = document.createElement("div");
        actions.className = "person-actions";

        const toggleButton = document.createElement("button");
        toggleButton.className = "person-toggle";
        toggleButton.type = "button";
        toggleButton.dataset.personId = person.person_id;
        toggleButton.dataset.active = person.active ? "false" : "true";
        toggleButton.textContent = person.active ? "Deaktivieren" : "Aktivieren";
        actions.appendChild(toggleButton);

        const deleteButton = document.createElement("button");
        deleteButton.className = "person-delete danger";
        deleteButton.type = "button";
        deleteButton.dataset.personId = person.person_id;
        deleteButton.textContent = "Löschen";
        actions.appendChild(deleteButton);

        card.appendChild(actions);
        peopleNode.appendChild(card);
      });
    }

    function renderWifiStatus(data) {
      if (!data.ok) {
        setResult("wifiStatus", data);
        return;
      }

      const devices = data.devices || [];
      const wlan0 = devices.find((device) => device.device === "wlan0");
      const wlan1 = devices.find((device) => device.device === "wlan1");
      const eth0 = devices.find((device) => device.device === "eth0");
      document.getElementById("wifiStatus").innerHTML =
          `<div class="wifi-summary">` +
          `<div>${pill("Internet wlan1: " + (wlan1 && wlan1.state === "connected" ? "verbunden" : "nicht verbunden"), wlan1 && wlan1.state === "connected")} ${escapeHtml(wlan1 && wlan1.connection ? wlan1.connection : "")}</div>` +
          `<div>${pill("Hotspot wlan0: " + (wlan0 && wlan0.state === "connected" ? "aktiv" : "nicht aktiv"), wlan0 && wlan0.state === "connected")} ${escapeHtml(wlan0 && wlan0.connection ? wlan0.connection : "")}</div>` +
          `<div>${pill("Ethernet eth0: " + (eth0 && eth0.state === "connected" ? "verbunden" : "nicht verbunden"), eth0 && eth0.state === "connected")} ${escapeHtml(eth0 && eth0.connection ? eth0.connection : "")}</div>` +
          `</div>`;

      const profilesNode = document.getElementById("wifiProfiles");
      profilesNode.innerHTML = "";
      const profiles = data.connections || [];
      if (profiles.length === 0) {
        profilesNode.textContent = "Keine gespeicherten WLAN-Profile gefunden.";
        return;
      }

      profiles.forEach((profile) => {
        const item = document.createElement("div");
        item.className = "wifi-item";
        const isActive = profile.device === "wlan1";
        const isHotspotProfile = profile.device === "wlan0";
        item.innerHTML =
            `<strong>${escapeHtml(profile.name)}</strong><br>` +
            `Gerät: ${escapeHtml(profile.device || "-")}<br>` +
            `Autoconnect: ${escapeHtml(profile.autoconnect || "-")}<br>` +
            `Priorität: ${escapeHtml(profile.priority || "0")}<br>` +
            `${isActive ? pill("aktuell auf wlan1 aktiv", true) : ""}` +
            `${isHotspotProfile ? pill("Hotspot-Profil geschützt", true) : ""}`;

        if (!isActive && !isHotspotProfile) {
          const activateButton = document.createElement("button");
          activateButton.type = "button";
          activateButton.textContent = "Auf wlan1 aktivieren";
          activateButton.addEventListener("click", () => activateWifiProfile(profile.name));
          item.appendChild(document.createElement("br"));
          item.appendChild(activateButton);
        }
        profilesNode.appendChild(item);
      });
    }

    function renderWifiNetworks(data) {
      const networksNode = document.getElementById("wifiNetworks");
      networksNode.innerHTML = "";
      if (!data.ok) {
        setResult("wifiResult", data);
        return;
      }

      const networks = data.networks || [];
      if (networks.length === 0) {
        networksNode.textContent = "Keine WLANs gefunden.";
        return;
      }

      networks.forEach((network) => {
        const item = document.createElement("div");
        item.className = "wifi-item";
        item.innerHTML =
            `<strong>${escapeHtml(network.ssid)}</strong><br>` +
            `Signal: ${escapeHtml(network.signal || "-")} %<br>` +
            `Sicherheit: ${escapeHtml(network.security || "-")}`;

        const useButton = document.createElement("button");
        useButton.type = "button";
        useButton.textContent = "SSID übernehmen";
        useButton.addEventListener("click", () => {
          document.getElementById("wifiSsid").value = network.ssid;
          if (!document.getElementById("wifiProfileName").value) {
            document.getElementById("wifiProfileName").value = network.ssid;
          }
          setResult("wifiResult", "SSID übernommen. Bitte Passwort eingeben und verbinden.");
        });
        item.appendChild(document.createElement("br"));
        item.appendChild(useButton);
        networksNode.appendChild(item);
      });
    }

    async function loadWifiStatus() {
      try {
        setResult("wifiStatus", "Lade WLAN-Status...");
        const response = await fetch("/api/wifi/status");
        const data = await response.json();
        renderWifiStatus(data);
      } catch (error) {
        setResult("wifiStatus", "Fehler beim Laden des WLAN-Status: " + error);
      }
    }

    async function scanWifiNetworks() {
      try {
        setResult("wifiResult", "Suche WLANs auf wlan1...");
        const response = await fetch("/api/wifi/scan", { method: "POST" });
        const data = await response.json();
        renderWifiNetworks(data);
      } catch (error) {
        setResult("wifiResult", "Fehler beim WLAN-Scan: " + error);
      }
    }

    async function activateWifiProfile(profileName) {
      try {
        setResult("wifiResult", `Aktiviere Profil ${profileName} auf wlan1...`);
        const response = await fetch("/api/wifi/activate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: profileName }),
        });
        const data = await response.json();
        setResult("wifiResult", data);
        await loadWifiStatus();
      } catch (error) {
        setResult("wifiResult", "Fehler beim Aktivieren des WLAN-Profils: " + error);
      }
    }

    async function setPersonActive(encodedPersonId, active) {
      try {
        setResult("enrollResult", active ? "Aktiviere Person..." : "Deaktiviere Person...");
        const response = await fetch(`/api/persons/${encodedPersonId}/active`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active }),
        });
        const data = await response.json();
        setResult("enrollResult", data);
        await loadStatus();
      } catch (error) {
        setResult("enrollResult", "Fehler beim Ändern des Personenstatus: " + error);
      }
    }

    async function deletePerson(encodedPersonId) {
      const personId = decodeURIComponent(encodedPersonId);
      if (!confirm(`Referenzbilder für "${personId}" wirklich löschen? Alte Ereignisse bleiben erhalten.`)) {
        return;
      }
      try {
        setResult("enrollResult", "Lösche Referenzbilder...");
        const response = await fetch(`/api/persons/${encodedPersonId}`, { method: "DELETE" });
        const data = await response.json();
        setResult("enrollResult", data);
        await loadStatus();
      } catch (error) {
        setResult("enrollResult", "Fehler beim Löschen: " + error);
      }
    }

    document.getElementById("people").addEventListener("click", async (event) => {
      const toggleButton = event.target.classList && event.target.classList.contains("person-toggle")
          ? event.target
          : null;
      if (toggleButton) {
        const encodedPersonId = encodeURIComponent(toggleButton.dataset.personId);
        await setPersonActive(encodedPersonId, toggleButton.dataset.active === "true");
        return;
      }

      const deleteButton = event.target.classList && event.target.classList.contains("person-delete")
          ? event.target
          : null;
      if (deleteButton) {
        const encodedPersonId = encodeURIComponent(deleteButton.dataset.personId);
        await deletePerson(encodedPersonId);
      }
    });

    document.getElementById("showEspEnroll").addEventListener("click", () => {
      showEnrollmentMethod("esp");
    });

    document.getElementById("showUploadEnroll").addEventListener("click", () => {
      showEnrollmentMethod("upload");
    });

    document.getElementById("configForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        setResult("configResult", "Speichere Telegram-Konfiguration...");
        const form = new FormData(event.currentTarget);
        const response = await fetch("/api/config", { method: "POST", body: form });
        const data = await response.json();
        setResult("configResult", data);
        document.getElementById("telegramBotToken").value = "";
        await loadStatus();
      } catch (error) {
        setResult("configResult", "Fehler beim Speichern: " + error);
      }
    });

    document.getElementById("thresholdForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        setResult("thresholdResult", "Speichere Zulassungsschwelle...");
        const form = new FormData(event.currentTarget);
        const response = await fetch("/api/config", { method: "POST", body: form });
        const data = await response.json();
        setResult("thresholdResult", data);
        await loadStatus();
      } catch (error) {
        setResult("thresholdResult", "Fehler beim Speichern der Schwelle: " + error);
      }
    });

    document.getElementById("testTelegramButton").addEventListener("click", async () => {
      try {
        setResult("configResult", "Sende Telegram-Testnachricht...");
        const response = await fetch("/api/test-telegram", { method: "POST" });
        const data = await response.json();
        setResult("configResult", data);
        await loadStatus();
      } catch (error) {
        setResult("configResult", "Fehler beim Telegram-Test: " + error);
      }
    });

    document.getElementById("shutdownButton").addEventListener("click", async () => {
      if (!confirm("Raspberry Pi wirklich sauber herunterfahren? Danach ist die Setup-Seite nicht mehr erreichbar.")) {
        return;
      }
      if (!confirm("Bitte bestätigen: Nach dem Herunterfahren erst dann den Strom trennen, wenn der Pi aus ist.")) {
        return;
      }

      try {
        setResult("systemResult", "Shutdown wird ausgelöst. Die Verbindung bricht gleich ab...");
        const response = await fetch("/api/system/shutdown", { method: "POST" });
        const data = await response.json();
        setResult("systemResult", data);
      } catch (error) {
        setResult("systemResult", "Shutdown wurde angefordert. Falls die Verbindung abbricht, ist das erwartbar: " + error);
      }
    });

    document.getElementById("wifiRefreshButton").addEventListener("click", loadWifiStatus);
    document.getElementById("wifiScanButton").addEventListener("click", scanWifiNetworks);

    document.getElementById("wifiConnectForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const ssid = String(form.get("ssid") || "").trim();
        const password = String(form.get("password") || "");
        const name = String(form.get("name") || "").trim();
        if (!ssid) {
          setResult("wifiResult", "Bitte eine SSID eingeben.");
          return;
        }
        if (!password) {
          setResult("wifiResult", "Bitte das WLAN-Passwort eingeben.");
          return;
        }

        setResult("wifiResult", `Verbinde wlan1 mit ${ssid}...`);
        const response = await fetch("/api/wifi/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ssid, password, name }),
        });
        const data = await response.json();
        setResult("wifiResult", data);
        document.getElementById("wifiPassword").value = "";
        await loadWifiStatus();
      } catch (error) {
        setResult("wifiResult", "Fehler beim Verbinden mit dem WLAN: " + error);
      }
    });

    document.getElementById("enrollForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitButton = event.currentTarget.querySelector("button[type='submit']");
      try {
        const personId = document.getElementById("personId").value.trim();
        const images = Array.from(document.getElementById("images").files || []);
        if (!personId) {
          setResult("enrollResult", "Bitte eine Person ID eingeben.");
          return;
        }
        if (!images || images.length === 0) {
          setResult("enrollResult", "Bitte mindestens ein Bild auswählen.");
          return;
        }
        submitButton.disabled = true;
        const results = [];

        for (let index = 0; index < images.length; index += 1) {
          const image = images[index];
          setResult("enrollResult", `Lade und analysiere Bild ${index + 1}/${images.length}: ${image.name}`);
          const form = new FormData();
          form.append("person_id", personId);
          form.append("note", `setup-upload:${image.name}`);
          form.append("image", image, image.name);

          try {
            const response = await fetch("/api/enroll", { method: "POST", body: form });
            const data = await response.json();
            results.push({ filename: image.name, status_code: response.status, ...data });
          } catch (error) {
            results.push({ filename: image.name, ok: false, error: String(error) });
          }

          setResult("enrollResult", {
            ok: results.some((result) => result.ok),
            person_id: personId,
            processed_images: index + 1,
            total_images: images.length,
            results,
          });
        }

        loadStatus().catch((error) => {
          document.getElementById("status").innerHTML = pill("Status-Refresh fehlgeschlagen: " + error, false);
        });
      } catch (error) {
        setResult("enrollResult", "Fehler beim Anlernen: " + error);
      } finally {
        submitButton.disabled = false;
      }
    });

    document.getElementById("espPreviewButton").addEventListener("click", () => {
      setResult("espEnrollResult", "Hole ESP-Kamerabild...");
      const preview = document.getElementById("espPreview");
      preview.onload = () => setResult("espEnrollResult", "ESP-Kamerabild geladen.");
      preview.onerror = () => setResult("espEnrollResult", "ESP-Kamerabild konnte nicht geladen werden.");
      preview.src = "/api/live-snapshot?t=" + Date.now();
    });

    function resetEspEnrollUi(message) {
      espEnrollState = {
        active: false,
        personId: "",
        sessionId: "",
        stepIndex: 0,
        results: [],
      };
      document.getElementById("espDiscardSessionButton").hidden = true;
      document.querySelector("#espEnrollForm button[type='submit']").textContent = "Geführte Aufnahme starten";
      if (message) {
        setEspGuide(message);
      }
    }

    async function fetchWithTimeout(url, options, timeoutMs) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, { ...(options || {}), signal: controller.signal });
      } finally {
        clearTimeout(timer);
      }
    }

    // Ein "TypeError: Load failed" / "Failed to fetch" oder ein AbortError bedeutet,
    // dass die Anfrage das Netzwerk nicht erfolgreich abschliessen konnte (Server
    // kurz nicht erreichbar, Neustart, Timeout). Das ist kein Anwendungsfehler.
    function isNetworkError(error) {
      return (
        (error && error.name === "AbortError") ||
        error instanceof TypeError
      );
    }

    function describeError(error) {
      if (error && error.name === "AbortError") {
        return "Zeitüberschreitung – der Server hat nicht rechtzeitig geantwortet.";
      }
      if (isNetworkError(error)) {
        return "Server nicht erreichbar (Verbindung unterbrochen oder Dienst neu gestartet).";
      }
      return String(error);
    }

    async function discardCurrentEspSession(confirmBeforeDiscard) {
      if (!espEnrollState.sessionId) {
        return { ok: false, error: "Keine aktive ESP-Anlernsession zum Verwerfen vorhanden." };
      }
      if (confirmBeforeDiscard && !confirm("Aktuelle ESP-Anlernsession wirklich verwerfen? Bereits gespeicherte Referenzen dieser Session werden gelöscht.")) {
        return { ok: false, cancelled: true };
      }

      try {
        const response = await fetchWithTimeout("/api/enroll-session/discard", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            person_id: espEnrollState.personId,
            session_id: espEnrollState.sessionId,
          }),
        }, 15000);
        return await response.json();
      } catch (error) {
        // Niemals nach aussen werfen: sonst entsteht ein zweites, verwirrendes
        // "Load failed" im Fehlerobjekt des Anlern-Flows.
        return { ok: false, unreachable: isNetworkError(error), error: describeError(error) };
      }
    }

    document.getElementById("espEnrollForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitButton = event.currentTarget.querySelector("button[type='submit']");
      const personId = document.getElementById("espPersonId").value.trim();
      if (!personId) {
        setResult("espEnrollResult", "Bitte eine Person ID eingeben.");
        return;
      }

      if (!espEnrollState.active || espEnrollState.personId !== personId) {
        espEnrollState = {
          active: true,
          personId,
          sessionId: "esp-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8),
          stepIndex: 0,
          results: [],
        };
        setEspGuide("Erster Schritt: " + espCaptureGuide[0] + " Bereite dich in Ruhe vor und klicke dann auf Aufnahme starten.");
        setResult("espEnrollResult", "Geführte Session vorbereitet. Es wurde noch kein Bild aufgenommen.");
        submitButton.textContent = "Aufnahme starten";
        document.getElementById("espDiscardSessionButton").hidden = false;
        return;
      }

      if (espEnrollState.stepIndex >= espCaptureGuide.length) {
        espEnrollState.active = false;
        submitButton.textContent = "Geführte Aufnahme starten";
        document.getElementById("espDiscardSessionButton").hidden = true;
        setEspGuide("Geführte Aufnahme ist bereits abgeschlossen. Starte erneut, wenn du weitere Referenzen aufnehmen möchtest.");
        return;
      }

      try {
        submitButton.disabled = true;
        const instruction = espCaptureGuide[espEnrollState.stepIndex];
        setEspGuide(instruction + " Wenn du bereit bist, wird jetzt genau ein Bild aufgenommen.");
        setResult("espEnrollResult", `Nehme Bild ${espEnrollState.stepIndex + 1}/12 auf...`);

        const form = new FormData();
        form.append("person_id", espEnrollState.personId);
        form.append("step", String(espEnrollState.stepIndex + 1));
        form.append("instruction", instruction);
        form.append("session_id", espEnrollState.sessionId);

        let response = null;
        let data = null;
        let networkError = null;
        const maxAttempts = 2;
        for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
          try {
            response = await fetchWithTimeout("/api/enroll-from-esp", { method: "POST", body: form }, 60000);
            data = await response.json();
            networkError = null;
            break;
          } catch (requestError) {
            networkError = requestError;
            if (!isNetworkError(requestError) || attempt >= maxAttempts) {
              break;
            }
            setResult("espEnrollResult", "Verbindung unterbrochen – neuer Versuch (" + (attempt + 1) + "/" + maxAttempts + ")...");
            await new Promise((resolve) => setTimeout(resolve, 1500));
          }
        }

        if (networkError) {
          if (!isNetworkError(networkError)) {
            throw networkError;
          }
          // Server war während der Aufnahme nicht erreichbar (z. B. kurzer
          // Neustart oder Timeout). Session NICHT verwerfen – ein Discard würde
          // ebenfalls fehlschlagen und bereits gute Referenzen könnten verloren
          // gehen. Stattdessen Schritt wiederholbar machen.
          submitButton.textContent = `Bild ${espEnrollState.stepIndex + 1}/12 erneut versuchen`;
          setEspGuide("Verbindung zum Server war kurz unterbrochen. Die Session bleibt erhalten – klicke erneut, um diesen Schritt ohne Datenverlust zu wiederholen.");
          setResult("espEnrollResult", {
            ok: false,
            error: "ESP-Anlernen fehlgeschlagen: " + describeError(networkError),
            failed_step: espEnrollState.stepIndex + 1,
            hinweis: "Verbindung unterbrochen – Schritt kann ohne Datenverlust wiederholt werden.",
          });
          return;
        }

        espEnrollState.results.push({
          step: espEnrollState.stepIndex + 1,
          instruction,
          status_code: response.status,
          ...data,
        });

        if (!response.ok || !data.ok) {
          const failedStep = espEnrollState.stepIndex + 1;
          const failedResults = espEnrollState.results.slice();
          const discardResult = await discardCurrentEspSession(false);
          resetEspEnrollUi("Aufnahme fehlgeschlagen. Die aktuelle Session wurde verworfen, damit keine unvollständigen Referenzen gespeichert bleiben.");
          setResult("espEnrollResult", {
            ok: false,
            error: data.error || "ESP-Aufnahme konnte nicht gespeichert werden.",
            failed_step: failedStep,
            discard_result: discardResult,
            results: failedResults,
          });
          loadStatus().catch((error) => {
            document.getElementById("status").innerHTML = pill("Status-Refresh fehlgeschlagen: " + error, false);
          });
          return;
        }

        if (data.latest_image_url) {
          document.getElementById("espPreview").src = data.latest_image_url + "?t=" + Date.now();
        }

        espEnrollState.stepIndex += 1;
        const complete = espEnrollState.stepIndex >= espCaptureGuide.length;
        setResult("espEnrollResult", {
          ok: espEnrollState.results.some((result) => result.ok),
          person_id: espEnrollState.personId,
          processed_images: espEnrollState.stepIndex,
          total_images: espCaptureGuide.length,
          next_instruction: complete ? null : espCaptureGuide[espEnrollState.stepIndex],
          results: espEnrollState.results,
        });

        if (complete) {
          setEspGuide("Alle 12 Bilder aufgenommen. Du kannst den Ablauf bei Bedarf erneut starten.");
          submitButton.textContent = "Geführte Aufnahme erneut starten";
          document.getElementById("espDiscardSessionButton").hidden = false;
          espEnrollState.active = false;
        } else {
          setEspGuide("Nächster Schritt: " + espCaptureGuide[espEnrollState.stepIndex] + " Bereite dich in Ruhe vor und klicke dann erneut.");
          submitButton.textContent = `Bild ${espEnrollState.stepIndex + 1}/12 aufnehmen`;
        }
        loadStatus().catch((error) => {
          document.getElementById("status").innerHTML = pill("Status-Refresh fehlgeschlagen: " + error, false);
        });
      } catch (error) {
        if (isNetworkError(error)) {
          // Netzwerkfehler: Session erhalten und Wiederholung ermöglichen,
          // statt einen weiteren (ebenfalls scheiternden) Discard auszulösen.
          submitButton.textContent = `Bild ${espEnrollState.stepIndex + 1}/12 erneut versuchen`;
          setEspGuide("Verbindung zum Server war kurz unterbrochen. Die Session bleibt erhalten – klicke erneut, um diesen Schritt ohne Datenverlust zu wiederholen.");
          setResult("espEnrollResult", {
            ok: false,
            error: "ESP-Anlernen fehlgeschlagen: " + describeError(error),
            hinweis: "Verbindung unterbrochen – Schritt kann ohne Datenverlust wiederholt werden.",
          });
        } else {
          const discardResult = espEnrollState.sessionId
            ? await discardCurrentEspSession(false)
            : null;
          resetEspEnrollUi("Aufnahmefehler. Die aktuelle Session wurde verworfen, damit keine unvollständigen Referenzen gespeichert bleiben.");
          setResult("espEnrollResult", {
            ok: false,
            error: "Fehler beim ESP-Anlernen: " + describeError(error),
            discard_result: discardResult,
          });
        }
      } finally {
        submitButton.disabled = false;
      }
    });

    document.getElementById("espDiscardSessionButton").addEventListener("click", async () => {
      try {
        setResult("espEnrollResult", "Verwerfe ESP-Anlernsession...");
        const data = await discardCurrentEspSession(true);
        if (data.cancelled) {
          setResult("espEnrollResult", "Verwerfen abgebrochen.");
          return;
        }
        setResult("espEnrollResult", data);
        resetEspEnrollUi("Session verworfen. Du kannst neu starten, wenn dein Gesicht gut sichtbar ist.");
        await loadStatus();
      } catch (error) {
        setResult("espEnrollResult", "Fehler beim Verwerfen der Session: " + error);
      }
    });

    loadStatus().catch((error) => {
      document.getElementById("status").innerHTML = pill("Statusfehler: " + error, false);
    });
    loadWifiStatus();
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


@dataclass
class AppConfig:
  telegram_bot_token: str = ""
  telegram_chat_id: str = ""
  esp_snapshot_url: str = DEFAULT_ESP_SNAPSHOT_URL
  similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD


class FaceVerifierService:
  def __init__(
      self,
      db_path: Path,
      config_path: Path = DEFAULT_CONFIG_PATH,
      model_name: str = DEFAULT_MODEL_NAME,
      similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
  ) -> None:
    self.db_path = Path(db_path)
    self.config_path = Path(config_path)
    self.captures_dir = DEFAULT_CAPTURES_DIR
    self.model_name = model_name
    self.similarity_threshold = similarity_threshold
    self.config = self._load_config(similarity_threshold)
    self.telegram_bot_token = self.config.telegram_bot_token
    self.telegram_chat_id = self.config.telegram_chat_id
    self.esp_snapshot_url = self.config.esp_snapshot_url
    self.similarity_threshold = self.config.similarity_threshold
    self._face_app: Optional[FaceAnalysis] = None
    self.captures_dir.mkdir(parents=True, exist_ok=True)
    self._ensure_default_threshold_persisted()

  def _ensure_default_threshold_persisted(self) -> None:
    """Stelle sicher, dass die Config eine Schwelle enthaelt.

    Standardwert ist 0.60. Wird nur geschrieben, wenn noch kein Wert in der
    Datei steht – ein bereits vom Nutzer gesetzter Wert bleibt unangetastet
    und wird nur auf ausdruecklichen Wunsch ueber die Oberflaeche geaendert.
    """
    existing: dict = {}
    if self.config_path.exists():
      try:
        loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
          existing = loaded
      except (OSError, json.JSONDecodeError) as exc:
        self._debug(f"config read for default persist failed: {exc}")
    if existing.get("similarity_threshold") is not None:
      return
    existing["similarity_threshold"] = self.similarity_threshold
    try:
      self.config_path.parent.mkdir(parents=True, exist_ok=True)
      self.config_path.write_text(
          json.dumps(existing, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
      )
      try:
        os.chmod(self.config_path, 0o600)
      except OSError as exc:
        self._debug(f"config chmod failed: {exc}")
      self._debug(
          f"Standard-Schwelle {self.similarity_threshold:.2f} in Config hinterlegt."
      )
    except OSError as exc:
      self._debug(f"default threshold persist failed: {exc}")

  @staticmethod
  def _debug(message: str) -> None:
    print(f"[face-verifier] {message}", flush=True)

  def _load_config(self, fallback_similarity_threshold: Optional[float] = None) -> AppConfig:
    payload = {}
    if self.config_path.exists():
      try:
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
      except (OSError, json.JSONDecodeError) as exc:
        self._debug(f"config load failed, using fallbacks: {exc}")

    telegram_bot_token = str(
        payload.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ).strip()
    telegram_chat_id = str(
        payload.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
    ).strip()
    esp_snapshot_url = str(
        payload.get("esp_snapshot_url") or os.environ.get("ESP_SNAPSHOT_URL", DEFAULT_ESP_SNAPSHOT_URL)
    ).strip()
    threshold_value = payload.get("similarity_threshold")
    if threshold_value is None:
      threshold_value = os.environ.get("SIMILARITY_THRESHOLD")
    try:
      similarity_threshold = float(
          threshold_value
          if threshold_value is not None and str(threshold_value).strip()
          else (
              fallback_similarity_threshold
              if fallback_similarity_threshold is not None
              else DEFAULT_SIMILARITY_THRESHOLD
          )
      )
    except (TypeError, ValueError):
      similarity_threshold = (
          fallback_similarity_threshold
          if fallback_similarity_threshold is not None
          else DEFAULT_SIMILARITY_THRESHOLD
      )
    similarity_threshold = min(max(similarity_threshold, 0.0), 1.0)

    return AppConfig(
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        esp_snapshot_url=esp_snapshot_url or DEFAULT_ESP_SNAPSHOT_URL,
        similarity_threshold=similarity_threshold,
    )

  def reload_config(self) -> None:
    self.config = self._load_config(self.similarity_threshold)
    self.telegram_bot_token = self.config.telegram_bot_token
    self.telegram_chat_id = self.config.telegram_chat_id
    self.esp_snapshot_url = self.config.esp_snapshot_url
    self.similarity_threshold = self.config.similarity_threshold

  def update_config(
      self,
      telegram_bot_token: Optional[str] = None,
      telegram_chat_id: Optional[str] = None,
      esp_snapshot_url: Optional[str] = None,
      similarity_threshold: Optional[str] = None,
  ) -> dict:
    current = self._load_config(self.similarity_threshold)
    next_similarity_threshold = current.similarity_threshold
    if similarity_threshold is not None and str(similarity_threshold).strip():
      try:
        next_similarity_threshold = min(max(float(similarity_threshold), 0.0), 1.0)
      except ValueError:
        raise ValueError("similarity_threshold muss eine Zahl zwischen 0 und 1 sein.")

    next_config = {
        "telegram_bot_token": (
            telegram_bot_token.strip()
            if telegram_bot_token is not None and telegram_bot_token.strip()
            else current.telegram_bot_token
        ),
        "telegram_chat_id": (
            telegram_chat_id.strip()
            if telegram_chat_id is not None and telegram_chat_id.strip()
            else current.telegram_chat_id
        ),
        "esp_snapshot_url": (
            esp_snapshot_url.strip()
            if esp_snapshot_url is not None and esp_snapshot_url.strip()
            else current.esp_snapshot_url
        ),
        "similarity_threshold": next_similarity_threshold,
    }

    self.config_path.write_text(json.dumps(next_config, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    try:
      os.chmod(self.config_path, 0o600)
    except OSError as exc:
      self._debug(f"config chmod failed: {exc}")

    self.reload_config()
    return self.config_status()

  def config_status(self) -> dict:
    return {
        "config_path": str(self.config_path),
        "config_exists": self.config_path.exists(),
        "telegram_enabled": self.telegram_enabled,
        "telegram_bot_token_masked": self._mask_secret(self.telegram_bot_token),
        "telegram_chat_id": self.telegram_chat_id,
        "esp_snapshot_url": self.esp_snapshot_url,
        "similarity_threshold": self.similarity_threshold,
        "setup_complete": self.setup_complete,
    }

  @property
  def setup_complete(self) -> bool:
    return self.telegram_enabled and len(self.list_person_ids()) > 0

  @staticmethod
  def _mask_secret(value: str) -> str:
    if not value:
      return ""
    if len(value) <= 8:
      return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"

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
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO reference_embeddings (person_id, embedding, note, created_at)
          VALUES (?, ?, ?, ?)
          """,
          (person_id, self._serialize_embedding(embedding), note, now),
      )
      connection.execute(
          """
          INSERT INTO person_settings (person_id, active, updated_at)
          VALUES (?, 1, ?)
          ON CONFLICT(person_id) DO UPDATE SET
            active = 1,
            updated_at = excluded.updated_at
          """,
          (person_id, now),
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

  def discard_enroll_session(self, person_id: str, session_id: str) -> dict:
    if not person_id:
      raise ValueError("person_id fehlt.")
    if not session_id:
      raise ValueError("session_id fehlt.")

    marker = f"session:{session_id}"
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO app_state (key, value)
          VALUES (?, ?)
          ON CONFLICT(key) DO UPDATE SET value = excluded.value
          """,
          (self._discarded_session_key(session_id), self._utc_now()),
      )
      cursor = connection.execute(
          """
          DELETE FROM reference_embeddings
          WHERE person_id = ?
            AND note LIKE ?
          """,
          (person_id, f"%{marker}%"),
      )
      deleted_references = cursor.rowcount
      remaining_references = connection.execute(
          "SELECT COUNT(*) FROM reference_embeddings WHERE person_id = ?",
          (person_id,),
      ).fetchone()[0]
      if remaining_references == 0:
        connection.execute(
            "DELETE FROM person_settings WHERE person_id = ?",
            (person_id,),
        )
      connection.commit()

    return {
        "person_id": person_id,
        "session_id": session_id,
        "deleted_references": deleted_references,
        "remaining_references": remaining_references,
    }

  def _discarded_session_key(self, session_id: str) -> str:
    return f"discarded_enroll_session:{session_id}"

  def _is_enroll_session_discarded(self, session_id: Optional[str]) -> bool:
    if not session_id:
      return False
    return self._get_app_state(self._discarded_session_key(session_id)) is not None

  def _person_is_active(self, person_id: str) -> bool:
    with sqlite3.connect(self.db_path) as connection:
      row = connection.execute(
          "SELECT active FROM person_settings WHERE person_id = ?",
          (person_id,),
      ).fetchone()
    return row is None or bool(row[0])

  def handle_ring_capture(
      self,
      person_id: Optional[str],
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

    self._debug(
        f"ring_capture start requested_person_id={person_id or 'ALL'} "
        f"sequence={sequence_index}/{total_images} event_id={event_id}"
    )

    result = self.verify(person_id=person_id, image_bytes=image_bytes, threshold=threshold)
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      if event_id is None:
        cursor = connection.execute(
            """
            INSERT INTO ring_events (person_id, total_images, received_images, matched_images, matched, best_similarity, created_at, updated_at)
            VALUES (?, ?, 0, 0, 0, NULL, ?, ?)
            """,
            (result.person_id, total_images, now, now),
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
      required_matches = min(DEFAULT_REQUIRED_MATCHES_FOR_ACCESS, total_images)
      event_matched = matched_images >= required_matches
      current_best = event_row[3]
      best_similarity = current_best
      if result.similarity is not None and (best_similarity is None or result.similarity > best_similarity):
        best_similarity = result.similarity

      connection.execute(
          """
          UPDATE ring_events
          SET person_id = ?, received_images = ?, matched_images = ?, matched = ?, best_similarity = ?, updated_at = ?
          WHERE id = ?
          """,
          (result.person_id, received_images, matched_images, int(event_matched), best_similarity, now, event_id),
      )
      connection.commit()

    event_complete = received_images >= total_images
    if event_complete:
      self._send_telegram_notification_for_event(event_id)

    self._debug(
        f"ring_capture result event_id={event_id} winner={result.person_id} "
        f"matched={result.matched} similarity={result.similarity} "
        f"matched_images={matched_images}/{required_matches} overall_matched={event_matched}"
    )

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
        "event_complete": event_complete,
        "matched_images": matched_images,
        "required_matches": required_matches,
        "overall_matched": event_matched,
        "overall_best_similarity": best_similarity,
        "image_url": f"/captures/{image_filename}",
    }

  def get_event_decision(self, event_id: int) -> dict:
    self._sync_telegram_updates()
    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      action = connection.execute(
          """
          SELECT event_id, decision, decision_source, decided_at
          FROM event_actions
          WHERE event_id = ?
          """,
          (event_id,),
      ).fetchone()

    decision = "pending"
    source = None
    decided_at = None
    if action is not None and action["decision"]:
      decision = action["decision"]
      source = action["decision_source"]
      decided_at = action["decided_at"]

    return {
        "ok": True,
        "event_id": event_id,
        "decision": decision,
        "decision_source": source,
        "decided_at": decided_at,
        "telegram_enabled": self.telegram_enabled,
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

  def fetch_live_snapshot(self, attempts: int = 3, retry_delay_s: float = 1.0) -> bytes:
    separator = "&" if "?" in self.esp_snapshot_url else "?"
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
      snapshot_url = f"{self.esp_snapshot_url}{separator}t={int(datetime.now(timezone.utc).timestamp() * 1000)}"
      try:
        with urlrequest.urlopen(snapshot_url, timeout=8) as response:
          return response.read()
      except Exception as exc:
        last_error = exc
        self._debug(f"snapshot fetch failed attempt={attempt}/{attempts}: {exc}")
        if attempt < attempts:
          time.sleep(retry_delay_s)

    raise last_error if last_error is not None else RuntimeError("Snapshot konnte nicht geladen werden.")

  def enroll_from_esp(
      self,
      person_id: str,
      count: int = 1,
      note: Optional[str] = None,
      session_id: Optional[str] = None,
  ) -> dict:
    if not person_id:
      raise ValueError("person_id fehlt.")
    if count < 1 or count > 10:
      raise ValueError("count muss zwischen 1 und 10 liegen.")

    self._debug(f"enroll_from_esp start person_id={person_id} count={count} url={self.esp_snapshot_url}")
    results = []
    success_count = 0
    latest_image_url = None

    for index in range(1, count + 1):
      try:
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        image_bytes = self.fetch_live_snapshot()
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        image_filename = (
            f"reference_{person_id}_{index}_{int(datetime.now(timezone.utc).timestamp() * 1000)}.jpg"
        )
        image_path = self.captures_dir / image_filename
        image_path.write_bytes(image_bytes)

        self._debug(
            f"enroll_from_esp image {index}/{count} bytes={len(image_bytes)} filename={image_filename}"
        )
        if self._is_enroll_session_discarded(session_id):
          raise RuntimeError("Anlernsession wurde verworfen.")

        payload = self.enroll_embedding(
            person_id=person_id,
            image_bytes=image_bytes,
            note=note or f"esp-camera:{image_filename}",
        )
        success_count += 1
        latest_image_url = f"/captures/{image_filename}"
        results.append({"ok": True, "image_url": latest_image_url, **payload})
        self._debug(
            f"enroll_from_esp image {index}/{count} ok reference_count={payload['reference_count']}"
        )
      except Exception as exc:
        self._debug(f"enroll_from_esp image {index}/{count} failed: {exc}")
        results.append({"ok": False, "error": str(exc)})

    self._debug(f"enroll_from_esp done person_id={person_id} successful={success_count}/{count}")
    return {
        "ok": success_count > 0,
        "person_id": person_id,
        "requested_images": count,
        "successful_images": success_count,
        "failed_images": count - success_count,
        "latest_image_url": latest_image_url,
        "results": results,
    }

  def network_status(self) -> dict:
    target_host = "api.telegram.org"
    status = {
        "internet_ok": False,
        "dns_ok": False,
        "tcp_ok": False,
        "target_host": target_host,
        "resolved_ip": None,
        "local_ip": None,
        "error": None,
    }

    try:
      probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      probe_socket.settimeout(2.0)
      probe_socket.connect(("1.1.1.1", 80))
      status["local_ip"] = probe_socket.getsockname()[0]
      probe_socket.close()
    except OSError as exc:
      status["error"] = f"Kein Uplink für Internet-Test: {exc}"
      return status

    try:
      resolved_ip = socket.gethostbyname(target_host)
      status["resolved_ip"] = resolved_ip
      status["dns_ok"] = True
    except OSError as exc:
      status["error"] = f"DNS-Auflösung für {target_host} fehlgeschlagen: {exc}"
      return status

    try:
      tcp_socket = socket.create_connection((target_host, 443), timeout=4.0)
      tcp_socket.close()
      status["tcp_ok"] = True
      status["internet_ok"] = True
    except OSError as exc:
      status["error"] = f"HTTPS-Verbindung zu {target_host}:443 fehlgeschlagen: {exc}"

    return status

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

  @property
  def telegram_enabled(self) -> bool:
    return bool(self.telegram_bot_token and self.telegram_chat_id)

  def _send_telegram_notification_for_event(self, event_id: int) -> None:
    if not self.telegram_enabled:
      return

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      self._debug(
          f"telegram skipped event_id={event_id}: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    with sqlite3.connect(self.db_path) as connection:
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

    event = self._event_to_dict(event_row)
    caption = self._build_telegram_caption(event, capture_row)
    image_path = self.captures_dir / capture_row["image_path"] if capture_row is not None else None

    try:
      message_id = self._telegram_send_photo(image_path, caption, event_id)
    except Exception as exc:
      self._debug(f"telegram send failed event_id={event_id}: {exc}")
      return

    now = self._utc_now()
    with sqlite3.connect(self.db_path) as connection:
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

  def _build_telegram_caption(self, event: dict, capture_row: Optional[sqlite3.Row]) -> str:
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

  def _telegram_api_url(self, method: str) -> str:
    return f"https://api.telegram.org/bot{self.telegram_bot_token}/{method}"

  def _telegram_reply_markup(self, event_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [[
                {"text": "Reinlassen", "callback_data": f"doorbell:approve:{event_id}"},
                {"text": "Ablehnen", "callback_data": f"doorbell:deny:{event_id}"},
            ]]
        }
    )

  def _telegram_send_photo(self, image_path: Optional[Path], caption: str, event_id: int) -> int:
    if image_path is not None and image_path.exists():
      with image_path.open("rb") as image_file:
        response = requests.post(
            self._telegram_api_url("sendPhoto"),
            data={
                "chat_id": self.telegram_chat_id,
                "caption": caption,
                "reply_markup": self._telegram_reply_markup(event_id),
            },
            files={"photo": image_file},
            timeout=15,
        )
    else:
      response = requests.post(
          self._telegram_api_url("sendMessage"),
          data={
              "chat_id": self.telegram_chat_id,
              "text": caption,
              "reply_markup": self._telegram_reply_markup(event_id),
          },
          timeout=15,
      )

    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
      raise RuntimeError(f"telegram api error: {payload}")
    return int(payload["result"]["message_id"])

  def send_telegram_test_message(self) -> dict:
    if not self.telegram_enabled:
      return {"ok": False, "error": "Telegram ist nicht vollstaendig konfiguriert."}

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      return {
          "ok": False,
          "error": "Kein Internet/Uplink für Telegram-Test.",
          "network": network_status,
      }

    try:
      response = requests.post(
          self._telegram_api_url("sendMessage"),
          data={
              "chat_id": self.telegram_chat_id,
              "text": "Smart Doorbell Telegram-Test: Verbindung funktioniert.",
          },
          timeout=15,
      )
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      return {"ok": False, "error": str(exc), "network": network_status}

    if not payload.get("ok"):
      return {"ok": False, "error": f"Telegram API meldet Fehler: {payload}", "network": network_status}

    return {"ok": True, "message": "Telegram-Testnachricht wurde gesendet.", "network": network_status}

  def request_safe_shutdown(self) -> dict:
    helper_path = Path(SAFE_POWEROFF_HELPER)
    if not helper_path.exists():
      return {
          "ok": False,
          "error": (
              f"Shutdown-Helper fehlt: {SAFE_POWEROFF_HELPER}. "
              "Bitte install_autostart.sh auf dem Pi erneut ausführen."
          ),
      }

    try:
      subprocess.Popen(
          ["sudo", "-n", SAFE_POWEROFF_HELPER],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          start_new_session=True,
      )
    except Exception as exc:
      return {
          "ok": False,
          "error": (
              f"Shutdown konnte nicht gestartet werden: {exc}. "
              "Bitte prüfen, ob die sudoers-Regel durch install_autostart.sh installiert wurde."
          ),
      }

    return {"ok": True, "message": "Shutdown wurde angefordert."}

  def wifi_setup(self, command: str, **kwargs) -> dict:
    helper_path = Path(WIFI_SETUP_HELPER)
    if not helper_path.exists():
      return {
          "ok": False,
          "error": (
              f"WLAN-Helper fehlt: {WIFI_SETUP_HELPER}. "
              "Bitte install_autostart.sh auf dem Pi erneut ausführen."
          ),
      }

    args = ["sudo", "-n", WIFI_SETUP_HELPER, command]
    if command == "connect":
      args.extend(["--ssid", str(kwargs.get("ssid") or "")])
      args.extend(["--password", str(kwargs.get("password") or "")])
      if kwargs.get("name"):
        args.extend(["--name", str(kwargs["name"])])
    elif command == "activate":
      args.extend(["--name", str(kwargs.get("name") or "")])
    elif command == "priority":
      args.extend(["--name", str(kwargs.get("name") or "")])
      args.extend(["--value", str(kwargs.get("value") or "")])
    elif command not in {"status", "scan"}:
      return {"ok": False, "error": "Unbekannter WLAN-Befehl."}

    try:
      result = subprocess.run(
          args,
          check=False,
          capture_output=True,
          text=True,
          timeout=55,
      )
    except Exception as exc:
      return {"ok": False, "error": str(exc)}

    output = (result.stdout or "").strip()
    try:
      payload = json.loads(output) if output else {}
    except json.JSONDecodeError:
      payload = {"ok": False, "error": output or result.stderr or "WLAN-Helper lieferte kein JSON."}

    if result.returncode != 0 and payload.get("ok", False):
      payload["ok"] = False
    if result.returncode != 0 and "error" not in payload:
      payload["error"] = (result.stderr or output or "WLAN-Helper fehlgeschlagen.").strip()
    return payload

  def _sync_telegram_updates(self) -> None:
    if not self.telegram_enabled:
      return

    network_status = self.network_status()
    if not network_status["internet_ok"]:
      self._debug(
          "telegram update sync skipped: kein Internet/Uplink. "
          f"details={json.dumps(network_status, ensure_ascii=True)}"
      )
      return

    offset = self._get_app_state("telegram_update_offset")
    params = {"timeout": 0, "allowed_updates": json.dumps(["callback_query"])}
    if offset is not None:
      params["offset"] = str(int(offset))

    try:
      response = requests.get(self._telegram_api_url("getUpdates"), params=params, timeout=15)
      response.raise_for_status()
      payload = response.json()
    except Exception as exc:
      self._debug(f"telegram update sync failed: {exc}")
      return

    if not payload.get("ok"):
      self._debug(f"telegram update sync returned error payload: {payload}")
      return

    next_offset = None
    for update in payload.get("result", []):
      next_offset = int(update["update_id"]) + 1
      callback_query = update.get("callback_query")
      if callback_query is not None:
        self._process_telegram_callback(callback_query)

    if next_offset is not None:
      self._set_app_state("telegram_update_offset", str(next_offset))

  def _process_telegram_callback(self, callback_query: dict) -> None:
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}

    if str(chat.get("id", "")) != self.telegram_chat_id:
      self._answer_callback_query(callback_id, "Dieser Chat ist nicht freigeschaltet.")
      return

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "doorbell" or parts[1] not in {"approve", "deny"}:
      self._answer_callback_query(callback_id, "Unbekannter Befehl.")
      return

    try:
      event_id = int(parts[2])
    except ValueError:
      self._answer_callback_query(callback_id, "Ungültige Ereignis-ID.")
      return

    decision = parts[1]
    now = self._utc_now()

    with sqlite3.connect(self.db_path) as connection:
      connection.row_factory = sqlite3.Row
      existing = connection.execute(
          "SELECT decision FROM event_actions WHERE event_id = ?",
          (event_id,),
      ).fetchone()
      if existing is not None and existing["decision"]:
        self._answer_callback_query(callback_id, "Dieses Klingeln wurde schon entschieden.")
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

    self._answer_callback_query(
        callback_id,
        "Zutritt freigegeben." if decision == "approve" else "Zutritt abgelehnt.",
    )

  def _answer_callback_query(self, callback_id: Optional[str], text: str) -> None:
    if not callback_id or not self.telegram_enabled:
      return
    try:
      response = requests.post(
          self._telegram_api_url("answerCallbackQuery"),
          data={"callback_query_id": callback_id, "text": text},
          timeout=15,
      )
      response.raise_for_status()
    except Exception as exc:
      self._debug(f"telegram callback answer failed: {exc}")

  def _get_app_state(self, key: str) -> Optional[str]:
    with sqlite3.connect(self.db_path) as connection:
      row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])

  def _set_app_state(self, key: str, value: str) -> None:
    with sqlite3.connect(self.db_path) as connection:
      connection.execute(
          """
          INSERT INTO app_state (key, value)
          VALUES (?, ?)
          ON CONFLICT(key) DO UPDATE SET value = excluded.value
          """,
          (key, value),
      )
      connection.commit()

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
    if not service.setup_complete:
      return redirect("/setup")
    response = make_response(DEFAULT_DASHBOARD_HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

  @app.get("/setup")
  def setup_page():
    response = make_response(DEFAULT_SETUP_HTML)
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
    mac = request.headers.get("X-ESP-MAC", "unknown")
    message = request.get_data(as_text=True).strip()
    if message:
      print(f"[ESP {mac}] {message}", flush=True)
    return jsonify({"ok": True})

  @app.get("/api/ring-decision")
  def ring_decision():
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

  return app


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Face verification prototype for Raspberry Pi 4.")
  parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Pfad zur SQLite-Datenbank.")
  parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH), help="Pfad zur lokalen Konfigurationsdatei.")
  parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="InsightFace-Modellname.")
  parser.add_argument(
      "--threshold",
      type=float,
      default=DEFAULT_SIMILARITY_THRESHOLD,
      help="Cosine-Similarity-Schwelle für match/no_match.",
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
