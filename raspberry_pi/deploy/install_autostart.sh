#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${1:-hs-iot-doorbell}"
# Dieses Script liegt unter <projekt>/deploy/, daher zeigt PROJECT_DIR auf das
# Elternverzeichnis (dort liegen run_doorbell.py und das Paket doorbell/).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
POWEROFF_HELPER="/usr/local/sbin/hs-iot-safe-poweroff"
WIFI_HELPER="/usr/local/sbin/hs-iot-wifi-setup"
SUDOERS_FILE="/etc/sudoers.d/hs-iot-safe-poweroff"
WIFI_SUDOERS_FILE="/etc/sudoers.d/hs-iot-wifi-setup"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

if [[ ! -f "${PROJECT_DIR}/run_doorbell.py" ]]; then
  echo "run_doorbell.py wurde in ${PROJECT_DIR} nicht gefunden." >&2
  exit 1
fi

if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
elif [[ -x "${PROJECT_DIR}/.iotp/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.iotp/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

TMP_SERVICE="$(mktemp)"
cat > "${TMP_SERVICE}" <<EOF
[Unit]
Description=HS IoT Smart Doorbell
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/run_doorbell.py serve --host 0.0.0.0 --port 8000
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
User=${RUN_USER}
Group=${RUN_GROUP}

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "${TMP_SERVICE}" "${SERVICE_FILE}"
rm -f "${TMP_SERVICE}"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

TMP_HELPER="$(mktemp)"
cat > "${TMP_HELPER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/systemctl poweroff
EOF
sudo install -m 0755 "${TMP_HELPER}" "${POWEROFF_HELPER}"
rm -f "${TMP_HELPER}"

TMP_WIFI_HELPER="$(mktemp)"
cat > "${TMP_WIFI_HELPER}" <<'EOF'
#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys

INTERNET_IFACE = "wlan1"
HOTSPOT_IFACE = "wlan0"


def nmcli(*args, timeout=20):
    nmcli_path = shutil.which("nmcli") or "/usr/bin/nmcli"
    result = subprocess.run(
        [nmcli_path, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "nmcli failed").strip())
    return result.stdout.strip()


def parse_table(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append([part.strip() for part in line.split(":")])
    return rows


def status():
    devices_rows = parse_table(nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"))
    connection_rows = parse_table(nmcli("-t", "-f", "NAME,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY", "connection", "show"))
    return {
        "internet_interface": INTERNET_IFACE,
        "hotspot_interface": HOTSPOT_IFACE,
        "devices": [
            {
                "device": row[0] if len(row) > 0 else "",
                "type": row[1] if len(row) > 1 else "",
                "state": row[2] if len(row) > 2 else "",
                "connection": row[3] if len(row) > 3 else "",
            }
            for row in devices_rows
        ],
        "connections": [
            {
                "name": row[0] if len(row) > 0 else "",
                "type": row[1] if len(row) > 1 else "",
                "device": row[2] if len(row) > 2 else "",
                "autoconnect": row[3] if len(row) > 3 else "",
                "priority": row[4] if len(row) > 4 else "",
            }
            for row in connection_rows
            if len(row) > 1 and row[1] == "wifi"
        ],
    }


def active_connection_for_interface(interface_name):
    for row in parse_table(nmcli("-t", "-f", "DEVICE,CONNECTION", "device", "status")):
        if len(row) > 1 and row[0] == interface_name:
            return row[1]
    return ""


def ensure_not_hotspot_profile(profile_name):
    hotspot_profile = active_connection_for_interface(HOTSPOT_IFACE)
    if hotspot_profile and profile_name == hotspot_profile:
        raise ValueError(
            f"Profil '{profile_name}' ist aktuell der lokale Hotspot auf {HOTSPOT_IFACE} "
            "und wird durch das Setup nicht verändert."
        )


def scan():
    nmcli("device", "wifi", "rescan", "ifname", INTERNET_IFACE, timeout=25)
    rows = parse_table(nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", INTERNET_IFACE, timeout=25))
    networks = []
    seen = set()
    for row in rows:
        ssid = row[1] if len(row) > 1 else ""
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append(
            {
                "in_use": (row[0] if len(row) > 0 else "") == "*",
                "ssid": ssid,
                "signal": row[2] if len(row) > 2 else "",
                "security": row[3] if len(row) > 3 else "",
            }
        )
    return {"interface": INTERNET_IFACE, "networks": networks}


def connect(ssid, password, name):
    if not ssid:
        raise ValueError("SSID fehlt.")
    if not password:
        raise ValueError("Passwort fehlt.")
    profile_name = name or ssid
    ensure_not_hotspot_profile(profile_name)
    try:
        output = nmcli(
            "device",
            "wifi",
            "connect",
            ssid,
            "password",
            password,
            "ifname",
            INTERNET_IFACE,
            "name",
            profile_name,
            timeout=45,
        )
    except RuntimeError as exc:
        if "already exists" not in str(exc) and "existiert bereits" not in str(exc):
            raise
        nmcli("connection", "modify", profile_name, "802-11-wireless.ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
        output = nmcli("connection", "up", profile_name, "ifname", INTERNET_IFACE, timeout=35)
    nmcli("connection", "modify", profile_name, "connection.autoconnect", "yes", "connection.autoconnect-priority", "20")
    return {"message": output, "profile": profile_name, "interface": INTERNET_IFACE}


def activate(name):
    if not name:
        raise ValueError("Profilname fehlt.")
    ensure_not_hotspot_profile(name)
    output = nmcli("connection", "up", name, "ifname", INTERNET_IFACE, timeout=35)
    return {"message": output, "profile": name, "interface": INTERNET_IFACE}


def priority(name, value):
    if not name:
        raise ValueError("Profilname fehlt.")
    priority_value = int(value)
    output = nmcli("connection", "modify", name, "connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority_value))
    return {"message": output, "profile": name, "priority": priority_value}


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("scan")

    connect_parser = subparsers.add_parser("connect")
    connect_parser.add_argument("--ssid", required=True)
    connect_parser.add_argument("--password", required=True)
    connect_parser.add_argument("--name", default="")

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--name", required=True)

    priority_parser = subparsers.add_parser("priority")
    priority_parser.add_argument("--name", required=True)
    priority_parser.add_argument("--value", required=True)

    args = parser.parse_args()
    try:
        if args.command == "status":
            payload = status()
        elif args.command == "scan":
            payload = scan()
        elif args.command == "connect":
            payload = connect(args.ssid, args.password, args.name)
        elif args.command == "activate":
            payload = activate(args.name)
        elif args.command == "priority":
            payload = priority(args.name, args.value)
        else:
            raise ValueError("Unbekannter Befehl.")
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
EOF
sudo install -m 0755 "${TMP_WIFI_HELPER}" "${WIFI_HELPER}"
rm -f "${TMP_WIFI_HELPER}"

TMP_SUDOERS="$(mktemp)"
cat > "${TMP_SUDOERS}" <<EOF
${RUN_USER} ALL=(root) NOPASSWD: ${POWEROFF_HELPER}
EOF
sudo visudo -cf "${TMP_SUDOERS}" >/dev/null
sudo install -m 0440 "${TMP_SUDOERS}" "${SUDOERS_FILE}"
rm -f "${TMP_SUDOERS}"

TMP_WIFI_SUDOERS="$(mktemp)"
cat > "${TMP_WIFI_SUDOERS}" <<EOF
${RUN_USER} ALL=(root) NOPASSWD: ${WIFI_HELPER}
EOF
sudo visudo -cf "${TMP_WIFI_SUDOERS}" >/dev/null
sudo install -m 0440 "${TMP_WIFI_SUDOERS}" "${WIFI_SUDOERS_FILE}"
rm -f "${TMP_WIFI_SUDOERS}"

echo "Autostart installiert: ${SERVICE_FILE}"
echo "Shutdown-Helper installiert: ${POWEROFF_HELPER}"
echo "WLAN-Helper installiert: ${WIFI_HELPER}"
echo "Projektordner: ${PROJECT_DIR}"
echo "Python: ${PYTHON_BIN}"
echo
sudo systemctl --no-pager --lines=20 status "${SERVICE_NAME}.service"
