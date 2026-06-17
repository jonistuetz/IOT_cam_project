"""Systemnahe Aktionen: Netzwerkstatus, sicheres Herunterfahren, WLAN-Setup.

Alle drei rufen externe Helfer bzw. Sockets auf und sind unabhängig von der
übrigen Fachlogik. Die WLAN-/Shutdown-Helfer werden von ``install_autostart.sh``
unter ``/usr/local/sbin`` installiert.
"""

import json
import socket
import subprocess
from pathlib import Path

from .constants import SAFE_POWEROFF_HELPER, WIFI_SETUP_HELPER


def network_status() -> dict:
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
    # UDP-"connect" sendet hier kein Nutzpaket. Es fragt das Routing des
    # Betriebssystems ab und liefert die lokale IP, über die Internetziele
    # erreicht würden.
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe_socket.settimeout(2.0)
    probe_socket.connect(("1.1.1.1", 80))
    status["local_ip"] = probe_socket.getsockname()[0]
    probe_socket.close()
  except OSError as exc:
    status["error"] = f"Kein Uplink für Internet-Test: {exc}"
    return status

  try:
    # DNS-Check getrennt vom TCP-Check, damit die Oberfläche genauer zeigen kann,
    # ob Namensauflösung oder eigentliche Verbindung das Problem ist.
    resolved_ip = socket.gethostbyname(target_host)
    status["resolved_ip"] = resolved_ip
    status["dns_ok"] = True
  except OSError as exc:
    status["error"] = f"DNS-Auflösung für {target_host} fehlgeschlagen: {exc}"
    return status

  try:
    # Reiner TCP-Verbindungsaufbau zu Port 443. Der echte TLS-Handshake und die
    # HTTP-Requests laufen später über requests in telegram.py.
    tcp_socket = socket.create_connection((target_host, 443), timeout=4.0)
    tcp_socket.close()
    status["tcp_ok"] = True
    status["internet_ok"] = True
  except OSError as exc:
    status["error"] = f"HTTPS-Verbindung zu {target_host}:443 fehlgeschlagen: {exc}"

  return status


def request_safe_shutdown() -> dict:
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


def wifi_setup(command: str, **kwargs) -> dict:
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
